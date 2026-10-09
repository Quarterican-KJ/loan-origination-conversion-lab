"""Pure value transformations (spec section 7).

Callers validate formats first; these functions assume their input already matches the contract
patterns and never guess, pad, or fill in missing values.
"""

from collections.abc import Iterable
from decimal import Context, Decimal, Inexact, Rounded, localcontext

# Any arithmetic that would round raises instead, whatever the caller's decimal context is.
_EXACT = Context(prec=60, traps=[Inexact, Rounded])
_RATE_PLACES = Decimal("0.0001")


def normalize_text(value: str) -> str:
    """Trim and collapse internal whitespace runs to single spaces. Case is kept."""
    return " ".join(value.split())


def business_name(name: str) -> str:
    return normalize_text(name)


def individual_name(first: str, middle_initial: str, last: str) -> str:
    """``FIRST [M.] LAST``. Only the middle initial's case changes."""
    parts = [normalize_text(first)]
    initial = middle_initial.strip()
    if initial:
        parts.append(f"{initial.upper()}.")
    parts.append(normalize_text(last))
    return " ".join(parts)


def parse_amount(text: str) -> Decimal:
    """``REQ_AMT`` text such as ``1250000.00``. Never passes through float."""
    return Decimal(text)


def parse_rate(text: str) -> Decimal:
    """``INT_RATE`` in thousandths of a percent: ``006875`` is ``Decimal("6.8750")``."""
    with localcontext(_EXACT):
        return Decimal(int(text)).scaleb(-3).quantize(_RATE_PLACES)


def parse_term(text: str) -> int:
    return int(text)


def exact_sum(values: Iterable[Decimal]) -> Decimal:
    with localcontext(_EXACT):
        return sum(values, Decimal("0.00"))
