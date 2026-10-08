from __future__ import annotations

from dataclasses import astuple, dataclass, fields

from sqlalchemy import Engine, FromClause, func, insert, select

from loan_lab.models import (
    ApplicationParty,
    Base,
    Borrower,
    Collateral,
    CollateralPledge,
    Lien,
    LoanApplication,
)
from loan_lab.synthetic.generator import DEFAULT_BATCH_SIZE, DEFAULT_SEED, SyntheticDataGenerator

# Parents before children, so foreign keys are satisfied within every batch.
INSERT_ORDER: tuple[tuple[str, FromClause], ...] = (
    ("borrowers", Borrower.__table__),
    ("applications", LoanApplication.__table__),
    ("collateral", Collateral.__table__),
    ("application_parties", ApplicationParty.__table__),
    ("collateral_pledges", CollateralPledge.__table__),
    ("liens", Lien.__table__),
)


@dataclass(frozen=True)
class SeedSummary:
    borrowers: int
    applications: int
    application_parties: int
    collateral: int
    collateral_pledges: int
    liens: int

    @property
    def total(self) -> int:
        return sum(astuple(self))

    def items(self) -> list[tuple[str, int]]:
        return [(f.name, getattr(self, f.name)) for f in fields(self)]


def count_rows(engine: Engine) -> SeedSummary:
    with engine.connect() as conn:
        counts = {
            name: conn.execute(select(func.count()).select_from(table)).scalar_one()
            for name, table in INSERT_ORDER
        }
    return SeedSummary(**counts)


def seed_database(
    engine: Engine,
    applications: int,
    *,
    seed: int = DEFAULT_SEED,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> SeedSummary:
    """Create the LOS tables if needed and insert synthetic data in a single transaction.

    Refuses to run if any LOS table already has rows; generated IDs start at 1.
    Returns counts read back from the database after commit.
    """
    Base.metadata.create_all(engine)
    if count_rows(engine).total:
        raise ValueError("Refusing to seed: the LOS tables already contain data")

    generator = SyntheticDataGenerator(applications, seed=seed, batch_size=batch_size)
    with engine.begin() as conn:
        for batch in generator.batches():
            for name, table in INSERT_ORDER:
                rows = getattr(batch, name)
                if rows:
                    conn.execute(insert(table), rows)
    return count_rows(engine)
