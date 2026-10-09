"""Phase 2: load a conversion plan into a new, isolated SQLite database (spec section 11).

The loader makes no data decisions. It inserts exactly the plan's eligible borrowers,
applications, and party relationships, in dependency order, inside one transaction. Exclusions,
rejections, and mappings were settled by the planner and are never re-evaluated here.
"""

import re
import secrets
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from sqlalchemy import Connection, Engine, Table, event, insert, text
from sqlalchemy.pool import NullPool

from loan_lab.conversion.legacy.contract import Rule
from loan_lab.conversion.legacy.plan import ConversionPlan
from loan_lab.conversion.legacy.source import RunIssue
from loan_lab.conversion.legacy.transforms import exact_sum
from loan_lab.db import create_db_engine
from loan_lab.models import ApplicationParty, Base, Borrower, LoanApplication
from loan_lab.paths import default_conversion_root

DATABASE_NAME = "loan_lab_conversion.db"
DEFAULT_LOAD_BATCH_SIZE = 1000
# A single path segment: no separators, no "..", so a run can only write below its root.
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

_BORROWERS: Table = Borrower.__table__  # type: ignore[assignment]
_APPLICATIONS: Table = LoanApplication.__table__  # type: ignore[assignment]
_PARTIES: Table = ApplicationParty.__table__  # type: ignore[assignment]


@dataclass(frozen=True)
class LoadCounts:
    borrowers: int
    applications: int
    parties: int


@dataclass(frozen=True)
class LoadResult:
    """What one committed load wrote, keyed for reconciliation (spec section 12)."""

    run_id: str
    database_path: Path
    # Source key -> database-generated ID, in load order.
    borrower_ids: Mapping[str, int]  # CUST_NO -> Borrower.id
    application_ids: Mapping[str, int]  # APPL_NO -> LoanApplication.id
    party_ids: Mapping[tuple[str, str], int]  # (APPL_NO, CUST_NO) -> ApplicationParty.id
    requested_amount: Decimal

    @property
    def counts(self) -> LoadCounts:
        return LoadCounts(len(self.borrower_ids), len(self.application_ids), len(self.party_ids))

    @property
    def borrowers_without_relationships(self) -> tuple[str, ...]:
        """Loaded customers with no loaded relationship; reconciled against WN-01 by RC-10."""
        related = {cust_no for _, cust_no in self.party_ids}
        return tuple(key for key in self.borrower_ids if key not in related)


class TargetPreconditionError(Exception):
    """RUN-07: the run's conversion database is not new and empty. Nothing was written to it."""

    def __init__(self, database_path: Path, message: str) -> None:
        self.database_path = database_path
        self.issue = RunIssue(Rule.RUN_07, str(database_path), message)
        super().__init__(str(self.issue))


class LoadStep(StrEnum):
    """Where a load was when it failed."""

    CREATE_SCHEMA = "create_schema"
    INSERT_BORROWERS = "insert_borrowers"
    INSERT_APPLICATIONS = "insert_applications"
    INSERT_PARTIES = "insert_parties"
    COMMIT = "commit"


class LoadFailedError(Exception):
    """The load transaction rolled back: the run fails at stage ``load`` with no rows committed.

    The conversion database is kept with its empty schema (or no schema, if creating it failed)
    as evidence, and its run ID is spent. ``__cause__`` holds the database or converter error.
    """

    def __init__(
        self, run_id: str, database_path: Path, planned: LoadCounts, planned_amount: Decimal,
        error: BaseException, step: LoadStep = LoadStep.CREATE_SCHEMA,
        schema_committed: bool = False,
    ) -> None:
        self.run_id = run_id
        self.database_path = database_path
        self.planned = planned
        self.planned_amount = planned_amount
        self.error = f"{type(error).__name__}: {error}"
        self.step = step
        # Only the schema transaction can have committed; the load transaction never has.
        self.schema_committed = schema_committed
        super().__init__(
            f"Load for run {run_id} failed at {step} and was rolled back; {database_path} holds "
            f"no converted rows. Planned {planned.borrowers} borrowers, {planned.applications} "
            f"applications, {planned.parties} parties, requested amount {planned_amount}. "
            f"Cause: {self.error}"
        )


def new_run_id() -> str:
    """A sortable, collision-resistant run ID such as ``20261009T184800Z-1a2b3c``."""
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def load_plan(
    plan: ConversionPlan,
    run_id: str,
    *,
    conversion_root: Path | None = None,
    batch_size: int = DEFAULT_LOAD_BATCH_SIZE,
) -> LoadResult:
    """Create ``<conversion_root>/<run_id>/loan_lab_conversion.db`` and load ``plan`` into it.

    Raises :class:`TargetPreconditionError` (RUN-07) if the run directory or database already
    exists, and :class:`LoadFailedError` if anything fails after the database was created.
    """
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"Invalid run ID {run_id!r}: use 1-64 letters, digits, '-' or '_'.")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    root = default_conversion_root() if conversion_root is None else conversion_root
    database_path = _create_database_file(root / run_id)

    planned = LoadCounts(
        len(plan.borrowers_to_load), len(plan.applications_to_load), len(plan.parties_to_load)
    )
    planned_amount = exact_sum(a.requested_amount for a in plan.applications_to_load)
    engine = _open(database_path)
    step = LoadStep.CREATE_SCHEMA
    schema_committed = False
    try:
        with engine.begin() as conn:
            _require_empty(conn, database_path)
            Base.metadata.create_all(conn)
        schema_committed = True
        with engine.begin() as conn:
            step = LoadStep.INSERT_BORROWERS
            borrower_ids = _insert(conn, _BORROWERS, _borrower_rows(plan), batch_size)
            step = LoadStep.INSERT_APPLICATIONS
            application_ids = _insert(conn, _APPLICATIONS, _application_rows(plan), batch_size)
            step = LoadStep.INSERT_PARTIES
            party_ids = _insert(
                conn, _PARTIES, _party_rows(plan, borrower_ids, application_ids), batch_size
            )
            step = LoadStep.COMMIT
    except TargetPreconditionError:
        raise
    except Exception as exc:
        raise LoadFailedError(
            run_id, database_path, planned, planned_amount, exc, step, schema_committed
        ) from exc
    finally:
        engine.dispose()

    return LoadResult(
        run_id=run_id,
        database_path=database_path,
        borrower_ids=MappingProxyType(borrower_ids),
        application_ids=MappingProxyType(application_ids),
        party_ids=MappingProxyType(party_ids),
        requested_amount=planned_amount,
    )


def _create_database_file(run_directory: Path) -> Path:
    """RUN-07, part 1: claim a run directory and database file that did not exist before."""
    database_path = run_directory / DATABASE_NAME
    try:
        run_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise TargetPreconditionError(
            database_path,
            f"Run directory {run_directory} already exists; a run ID is never reused.",
        ) from None
    # Exclusive creation fails rather than reuse a file that appeared in the meantime.
    try:
        database_path.open("xb").close()
    except FileExistsError:
        raise TargetPreconditionError(
            database_path, "The conversion database already exists; it is never overwritten."
        ) from None
    return database_path


def _open(database_path: Path) -> Engine:
    # NullPool closes the file when each connection ends, so nothing holds it after the load.
    engine = create_db_engine(f"sqlite:///{database_path.as_posix()}", poolclass=NullPool)
    # pysqlite would otherwise run DDL outside the transaction and begin lazily; issue BEGIN
    # ourselves so schema creation and the load are each fully atomic.
    event.listen(engine, "connect", _disable_driver_transactions)
    event.listen(engine, "begin", _begin_immediate)
    return engine


def _disable_driver_transactions(dbapi_connection: Any, _connection_record: Any) -> None:
    dbapi_connection.isolation_level = None


def _begin_immediate(conn: Connection) -> None:
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def _require_empty(conn: Connection, database_path: Path) -> None:
    """RUN-07, part 2: the new database holds no schema objects, so it cannot hold LOS rows."""
    objects = conn.execute(text("SELECT count(*) FROM sqlite_master")).scalar_one()
    if objects:
        raise TargetPreconditionError(
            database_path, f"The conversion database is not empty ({objects} schema objects)."
        )


def _borrower_rows(plan: ConversionPlan) -> Iterator[tuple[str, dict[str, Any]]]:
    for borrower in plan.borrowers_to_load:
        yield borrower.source_system_id, {
            "source_system": borrower.source_system,
            "source_system_id": borrower.source_system_id,
            "legal_name": borrower.legal_name,
            "borrower_type": borrower.borrower_type,
        }


def _application_rows(plan: ConversionPlan) -> Iterator[tuple[str, dict[str, Any]]]:
    for application in plan.applications_to_load:
        yield application.source_system_id, {
            "source_system": application.source_system,
            "source_system_id": application.source_system_id,
            "loan_product": application.loan_product,
            "requested_amount": application.requested_amount,
            "interest_rate": application.interest_rate,
            "term_months": application.term_months,
            "status": application.status,
        }


def _party_rows(
    plan: ConversionPlan, borrower_ids: Mapping[str, int], application_ids: Mapping[str, int]
) -> Iterator[tuple[tuple[str, str], dict[str, Any]]]:
    for party in plan.parties_to_load:
        key = (party.application_source_id, party.borrower_source_id)
        # A party whose parent is not loaded means the plan is inconsistent: fail, never skip.
        if party.application_source_id not in application_ids:
            raise LookupError(f"Party {key} references application {key[0]}, which is not loaded.")
        if party.borrower_source_id not in borrower_ids:
            raise LookupError(f"Party {key} references borrower {key[1]}, which is not loaded.")
        yield key, {
            "application_id": application_ids[party.application_source_id],
            "borrower_id": borrower_ids[party.borrower_source_id],
            "role": party.role,
        }


def _insert[K](
    conn: Connection, table: Table, rows: Iterable[tuple[K, dict[str, Any]]], batch_size: int
) -> dict[K, int]:
    """Insert in batches and return source key -> generated ID, in insertion order."""
    ids: dict[K, int] = {}
    statement = insert(table).returning(table.c.id, sort_by_parameter_order=True)
    for batch in _batches(rows, batch_size):
        keys = [key for key, _ in batch]
        if len(set(keys)) != len(keys) or not ids.keys().isdisjoint(keys):
            raise ValueError(f"Duplicate source key in the {table.name} load plan.")
        generated = conn.execute(statement, [values for _, values in batch]).scalars().all()
        ids.update(zip(keys, generated, strict=True))
    return ids


def _batches[T](items: Iterable[T], size: int) -> Iterator[Sequence[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch
