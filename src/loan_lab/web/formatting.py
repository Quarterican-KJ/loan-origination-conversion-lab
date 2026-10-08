"""Jinja filters. Missing values render as words, never as zero."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

UNKNOWN = "Unknown"

_LABEL_OVERRIDES = {
    "co_borrower": "Co-borrower",
    "commercial_real_estate": "Commercial real estate",
}


def money(value: Decimal | None, missing: str = UNKNOWN) -> str:
    if value is None:
        return missing
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def compact_money(value: Decimal) -> str:
    """Abbreviated amount for summary cards: $3.30B, $659.2K."""
    for threshold, suffix in ((10**9, "B"), (10**6, "M"), (10**3, "K")):
        if abs(value) >= threshold:
            return f"${value / threshold:,.2f}{suffix}"
    return money(value)


def rate(value: Decimal | None) -> str:
    return UNKNOWN if value is None else f"{value:.4f}%"


def iso_date(value: date | None) -> str:
    return UNKNOWN if value is None else value.isoformat()


def number(value: int) -> str:
    return f"{value:,}"


def percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def label(value: StrEnum | str | None) -> str:
    if value is None:
        return UNKNOWN
    text = str(value)
    return _LABEL_OVERRIDES.get(text, text.replace("_", " ").capitalize())


def term(months: int) -> str:
    years, remainder = divmod(months, 12)
    if remainder == 0:
        return f"{months} mo ({years} yr)"
    return f"{months} mo"


FILTERS = {
    "money": money,
    "compact_money": compact_money,
    "rate": rate,
    "iso_date": iso_date,
    "number": number,
    "percent": percent,
    "label": label,
    "term": term,
}
