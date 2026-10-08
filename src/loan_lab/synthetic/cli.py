"""Command line entry point: python -m loan_lab.synthetic --preset small"""

from __future__ import annotations

import argparse
import sys
import time
import tomllib
from pathlib import Path

from sqlalchemy import Engine, func, inspect, select

from loan_lab.db import create_db_engine
from loan_lab.models import Base
from loan_lab.synthetic.generator import DEFAULT_BATCH_SIZE, DEFAULT_SEED, PRESETS
from loan_lab.synthetic.seeding import seed_database

PROJECT_NAME = "loan-origination-conversion-lab"
DEFAULT_DATABASE_RELATIVE = Path("data") / "loan_lab_dev.db"

EXIT_OK = 0
EXIT_REFUSED = 1

LABELS = {
    "borrowers": "Borrowers",
    "applications": "Loan applications",
    "application_parties": "Application parties",
    "collateral": "Collateral",
    "collateral_pledges": "Collateral pledges",
    "liens": "Liens",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m loan_lab.synthetic",
        description="Seed the SQLite development database with deterministic synthetic LOS data.",
    )
    parser.add_argument(
        "--preset",
        required=True,
        choices=list(PRESETS),
        help="small = 25, demo = 5,000, stress = 100,000 loan applications",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help=(
            f"SQLite file to seed; relative paths are relative to the current directory "
            f"(default: <project root>/{DEFAULT_DATABASE_RELATIVE.as_posix()})"
        ),
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="random seed")
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=DEFAULT_BATCH_SIZE,
        help="applications generated and inserted per batch",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete all existing LOS data in the database before seeding (asks for confirmation)",
    )
    parser.add_argument(
        "--confirm-reset",
        metavar="FILE_NAME",
        help="non-interactive reset confirmation; must equal the database file name",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.confirm_reset is not None and not args.reset:
        parser.error("--confirm-reset requires --reset")

    if args.database is not None:
        path: Path = args.database
    else:
        try:
            path = default_database_path()
        except FileNotFoundError as error:
            parser.error(f"{error} Pass --database explicitly.")
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_db_engine(f"sqlite:///{path.resolve().as_posix()}")
    try:
        existing_rows, foreign_tables = _inspect_existing(engine)
        if foreign_tables:
            print(
                f"Refusing to seed {path}: it contains tables not managed by loan_lab "
                f"({', '.join(foreign_tables)}). Choose a dedicated development database file.",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        if existing_rows:
            if not args.reset:
                print(
                    f"Refusing to seed {path}: it already contains {existing_rows:,} LOS rows. "
                    "Rerun with --reset to delete them and regenerate.",
                    file=sys.stderr,
                )
                return EXIT_REFUSED
            if not _confirm_reset(path, existing_rows, args.confirm_reset):
                print("Reset not confirmed. The database was not changed.", file=sys.stderr)
                return EXIT_REFUSED
            Base.metadata.drop_all(engine)
            print(f"Deleted {existing_rows:,} existing LOS rows from {path}.")

        started = time.perf_counter()
        summary = seed_database(
            engine, PRESETS[args.preset], seed=args.seed, batch_size=args.batch_size
        )
        elapsed = time.perf_counter() - started
    finally:
        engine.dispose()

    print(
        f"Seeded {path} with synthetic data "
        f"(preset={args.preset}, seed={args.seed}, batch size={args.batch_size}) "
        f"in {elapsed:.1f}s:"
    )
    for name, count in summary.items():
        print(f"  {LABELS[name]:<22}{count:>10,}")
    return EXIT_OK


def find_project_root(start: Path) -> Path:
    """Return the nearest ancestor of `start` whose pyproject.toml declares this project."""
    for directory in start.resolve().parents:
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            with pyproject.open("rb") as file:
                name = tomllib.load(file).get("project", {}).get("name")
            if name == PROJECT_NAME:
                return directory
    raise FileNotFoundError(f"Could not find the {PROJECT_NAME} project root above {start}.")


def default_database_path() -> Path:
    """The development database under the project root, independent of the working directory."""
    return find_project_root(Path(__file__)) / DEFAULT_DATABASE_RELATIVE


def _inspect_existing(engine: Engine) -> tuple[int, list[str]]:
    """Return (rows in LOS tables, names of non-LOS tables) for the target database."""
    table_names = inspect(engine).get_table_names()
    foreign = sorted(name for name in table_names if name not in Base.metadata.tables)
    rows = 0
    with engine.connect() as conn:
        for name in table_names:
            if name in Base.metadata.tables:
                table = Base.metadata.tables[name]
                rows += conn.execute(select(func.count()).select_from(table)).scalar_one()
    return rows, foreign


def _confirm_reset(path: Path, existing_rows: int, confirmation: str | None) -> bool:
    expected = path.name
    if confirmation is not None:
        return confirmation == expected
    if not sys.stdin.isatty():
        print(
            f"--reset needs confirmation. Pass --confirm-reset {expected} "
            "when running non-interactively.",
            file=sys.stderr,
        )
        return False
    answer = input(
        f"This permanently deletes {existing_rows:,} LOS rows from {path.resolve()}.\n"
        f"Type the database file name ({expected}) to confirm: "
    )
    return answer.strip() == expected


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number
