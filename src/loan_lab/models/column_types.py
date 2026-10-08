from decimal import MAX_PREC, ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Dialect, Integer, Numeric
from sqlalchemy.types import TypeDecorator, TypeEngine

# Private context so results never depend on the caller's decimal.getcontext() settings.
_EXACT = Context(prec=MAX_PREC, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation])

SQLITE_INTEGER_MIN = -(2**63)
SQLITE_INTEGER_MAX = 2**63 - 1


class ExactDecimal(TypeDecorator[Decimal]):
    """Fixed-point decimal column that never round-trips through float.

    SQLite has no exact decimal storage (NUMERIC values are kept as REAL), so on SQLite
    the value is stored as a signed 64-bit INTEGER equal to value * 10**scale. For
    ExactDecimal(15, 2), Decimal("1234.56") is stored as 123456. Raw SQL must account
    for that scale; see docs/architecture.md. Other dialects use NUMERIC(precision, scale).

    Floats, NaN, Infinity, values with more than `scale` decimal places, and values
    outside the precision (or SQLite's 64-bit range) are rejected, never rounded.
    Stored SQLite values that are not in-range integers are rejected on read.
    """

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int, scale: int) -> None:
        super().__init__(precision=precision, scale=scale, asdecimal=True)
        self.precision = precision
        self.scale = scale
        self._quantum = Decimal(1).scaleb(-scale, context=_EXACT)
        self._limit = Decimal(1).scaleb(precision - scale, context=_EXACT)

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Integer())
        return dialect.type_descriptor(Numeric(self.precision, self.scale, asdecimal=True))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if isinstance(value, (float, bool)):
            raise TypeError(f"Use Decimal, int, or str for exact values, not {type(value).__name__}")
        exact = Decimal(value)
        if not exact.is_finite():
            raise ValueError(f"{value!r} is not a finite number")
        if exact.copy_abs() >= self._limit:
            raise ValueError(f"{value!r} exceeds NUMERIC({self.precision}, {self.scale})")
        quantized = exact.quantize(self._quantum, context=_EXACT)
        if quantized != exact:
            raise ValueError(f"{value!r} has more than {self.scale} decimal places")
        if dialect.name == "sqlite":
            scaled = int(quantized.scaleb(self.scale, context=_EXACT))
            if not SQLITE_INTEGER_MIN <= scaled <= SQLITE_INTEGER_MAX:
                raise ValueError(f"{value!r} does not fit SQLite's signed 64-bit INTEGER when scaled")
            return scaled
        return quantized

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if dialect.name == "sqlite":
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(
                    f"Expected a scaled INTEGER from SQLite, got {type(value).__name__} {value!r}"
                )
            result = Decimal(value).scaleb(-self.scale, context=_EXACT)
        else:
            result = Decimal(value)
        if result.copy_abs() >= self._limit:
            raise ValueError(f"Stored value {value!r} exceeds NUMERIC({self.precision}, {self.scale})")
        return result.quantize(self._quantum, context=_EXACT)
