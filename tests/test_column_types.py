from collections.abc import Iterator
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Decimal, InvalidOperation, localcontext

import pytest
from sqlalchemy import Column, Engine, Integer, MetaData, Table, func, insert, select, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import StatementError

from loan_lab.db import create_db_engine
from loan_lab.models import LoanApplication
from loan_lab.models.column_types import ExactDecimal

SQLITE = sqlite.dialect()
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

MONEY = ExactDecimal(15, 2)
RATE = ExactDecimal(7, 4)
WIDE = ExactDecimal(19, 0)

metadata = MetaData()
amounts = Table(
    "amounts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("money", MONEY),
    Column("rate", RATE),
    Column("wide", WIDE),
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = create_db_engine("sqlite://")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def round_trip(engine: Engine, column: str, value: object) -> tuple[object, Decimal]:
    """Write through the type, then return (raw stored value, value read back through the type)."""
    with engine.begin() as conn:
        row_id = conn.execute(insert(amounts).values({column: value})).inserted_primary_key[0]
        raw = conn.execute(
            text(f"SELECT {column} FROM amounts WHERE id = :id"), {"id": row_id}
        ).scalar_one()
        read = conn.execute(select(amounts.c[column]).where(amounts.c.id == row_id)).scalar_one()
    return raw, read


def test_model_columns_use_expected_precision_and_scale() -> None:
    table = LoanApplication.__table__
    assert (table.c.requested_amount.type.precision, table.c.requested_amount.type.scale) == (15, 2)
    assert (table.c.interest_rate.type.precision, table.c.interest_rate.type.scale) == (7, 4)


@pytest.mark.parametrize(
    ("value", "stored", "expected"),
    [
        (Decimal("0"), 0, "0.00"),
        (Decimal("0.00"), 0, "0.00"),
        (Decimal("-0.00"), 0, "0.00"),
        (Decimal("0.01"), 1, "0.01"),
        (Decimal("1234.56"), 123456, "1234.56"),
        (Decimal("1.500"), 150, "1.50"),
        (5, 500, "5.00"),
        ("250000.00", 25000000, "250000.00"),
        (Decimal("-0.01"), -1, "-0.01"),
        (Decimal("-1234.56"), -123456, "-1234.56"),
        (Decimal("9999999999999.99"), 999999999999999, "9999999999999.99"),
        (Decimal("-9999999999999.99"), -999999999999999, "-9999999999999.99"),
    ],
)
def test_money_round_trips_exactly(
    engine: Engine, value: object, stored: int, expected: str
) -> None:
    raw, read = round_trip(engine, "money", value)

    assert raw == stored
    assert type(raw) is int
    assert str(read) == expected


@pytest.mark.parametrize("value", ["10000000000000.00", "-10000000000000.00", "9999999999999.995"])
def test_money_beyond_precision_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        MONEY.process_bind_param(Decimal(value), SQLITE)


@pytest.mark.parametrize(
    ("value", "stored", "expected"),
    [
        (Decimal("6.1250"), 61250, "6.1250"),
        (Decimal("0.0001"), 1, "0.0001"),
        (Decimal("12.3456"), 123456, "12.3456"),
        (Decimal("0.0000"), 0, "0.0000"),
        (Decimal("7"), 70000, "7.0000"),
        (Decimal("999.9999"), 9999999, "999.9999"),
    ],
)
def test_interest_rate_with_four_decimal_places_round_trips_exactly(
    engine: Engine, value: Decimal, stored: int, expected: str
) -> None:
    raw, read = round_trip(engine, "rate", value)

    assert raw == stored
    assert str(read) == expected


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (1.5, TypeError),
        (0.0, TypeError),
        (float("nan"), TypeError),
        (float("inf"), TypeError),
        (float("-inf"), TypeError),
        (True, TypeError),
        (Decimal("NaN"), ValueError),
        (Decimal("-NaN"), ValueError),
        (Decimal("sNaN"), ValueError),
        (Decimal("Infinity"), ValueError),
        (Decimal("-Infinity"), ValueError),
        ("NaN", ValueError),
        ("Infinity", ValueError),
        (Decimal("0.001"), ValueError),
        (Decimal("100.001"), ValueError),
        (Decimal("-0.005"), ValueError),
        ("1.23456789", ValueError),
    ],
)
def test_inexact_or_non_finite_money_is_rejected(value: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        MONEY.process_bind_param(value, SQLITE)


@pytest.mark.parametrize("value", [Decimal("6.12345"), Decimal("0.00001"), 6.125])
def test_rate_with_excess_precision_or_float_is_rejected(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        RATE.process_bind_param(value, SQLITE)


def test_rejected_value_is_not_written(engine: Engine) -> None:
    with pytest.raises(StatementError):
        with engine.begin() as conn:
            conn.execute(insert(amounts).values(money=Decimal("1.005")))

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM amounts")).scalar_one() == 0


@pytest.mark.parametrize("value", [INT64_MAX, INT64_MIN])
def test_wide_values_at_int64_limits_round_trip(engine: Engine, value: int) -> None:
    raw, read = round_trip(engine, "wide", Decimal(value))

    assert raw == value
    assert read == Decimal(value)


@pytest.mark.parametrize("value", [INT64_MAX + 1, INT64_MIN - 1, 10**19 - 1])
def test_values_outside_sqlite_int64_are_rejected_before_the_driver(value: int) -> None:
    with pytest.raises(ValueError, match="64-bit"):
        WIDE.process_bind_param(Decimal(value), SQLITE)


def test_scaled_value_outside_int64_is_rejected() -> None:
    wide_money = ExactDecimal(20, 2)

    assert wide_money.process_bind_param(Decimal("92233720368547758.07"), SQLITE) == INT64_MAX
    with pytest.raises(ValueError, match="64-bit"):
        wide_money.process_bind_param(Decimal("92233720368547758.08"), SQLITE)


@pytest.mark.parametrize("rounding", [ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP])
@pytest.mark.parametrize(
    "value", [Decimal("9999999999999.99"), Decimal("-9999999999999.99"), Decimal("1234567.89")]
)
def test_reduced_decimal_context_does_not_affect_round_trip(
    engine: Engine, value: Decimal, rounding: str
) -> None:
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.rounding = rounding
        raw, read = round_trip(engine, "money", value)

    assert raw == int(value.scaleb(2))
    assert read == value
    assert str(read) == str(value)


def test_reduced_decimal_context_still_rejects_excess_precision() -> None:
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.traps[InvalidOperation] = False
        with pytest.raises(ValueError):
            MONEY.process_bind_param(Decimal("1234567.891"), SQLITE)
        with pytest.raises(ValueError):
            MONEY.process_bind_param(Decimal("1.005"), SQLITE)


def test_reduced_decimal_context_reads_stored_value_exactly() -> None:
    with localcontext() as ctx:
        ctx.prec = 6
        assert MONEY.process_result_value(999999999999999, SQLITE) == Decimal("9999999999999.99")


@pytest.mark.parametrize("raw", [12.5, 1e20])
def test_non_integer_sqlite_value_is_rejected_on_read(engine: Engine, raw: float) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO amounts (money) VALUES (:raw)"), {"raw": raw})

    with pytest.raises(TypeError):
        with engine.connect() as conn:
            conn.execute(select(amounts.c.money)).scalar_one()


def test_stored_integer_beyond_precision_is_rejected_on_read(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO amounts (money) VALUES (:raw)"), {"raw": 10**15})

    with pytest.raises(ValueError):
        with engine.connect() as conn:
            conn.execute(select(amounts.c.money)).scalar_one()


def test_raw_sql_sees_scaled_integers_but_typed_aggregates_do_not(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(insert(amounts), [{"money": Decimal("100.10")}, {"money": Decimal("0.25")}])
        raw_total = conn.execute(text("SELECT SUM(money) FROM amounts")).scalar_one()
        raw_filtered = conn.execute(
            text("SELECT COUNT(*) FROM amounts WHERE money > :threshold"),
            {"threshold": 10000},
        ).scalar_one()
        typed_total = conn.execute(select(func.sum(amounts.c.money))).scalar_one()
        typed_filtered = conn.execute(
            select(func.count()).where(amounts.c.money > Decimal("100.00"))
        ).scalar_one()

    assert raw_total == 10035
    assert Decimal(raw_total).scaleb(-2) == Decimal("100.35")
    assert raw_filtered == 1
    assert typed_total == Decimal("100.35")
    assert typed_filtered == 1
