"""Legacy LOS CSV conversion, phase 1: read, validate, and map into an immutable plan.

The contract is docs/conversion-specification.md. Loading and reconciliation are not implemented;
they will consume :class:`ConversionPlan` without changing it.
"""

from loan_lab.conversion.legacy.contract import SOURCE_SYSTEM, Rule
from loan_lab.conversion.legacy.plan import (
    AmountTotals,
    ApplicationUnit,
    Cause,
    ConversionPlan,
    Disposition,
    Issue,
    MappedApplication,
    MappedBorrower,
    MappedParty,
    Outcome,
    RowResult,
    UnmappedField,
)
from loan_lab.conversion.legacy.planner import build_plan, plan_conversion
from loan_lab.conversion.legacy.source import (
    ControlTotals,
    RunIssue,
    SourceExtract,
    SourceRef,
    SourceValidationError,
    read_extract,
)

__all__ = [
    "SOURCE_SYSTEM",
    "AmountTotals",
    "ApplicationUnit",
    "Cause",
    "ControlTotals",
    "ConversionPlan",
    "Disposition",
    "Issue",
    "MappedApplication",
    "MappedBorrower",
    "MappedParty",
    "Outcome",
    "RowResult",
    "Rule",
    "RunIssue",
    "SourceExtract",
    "SourceRef",
    "SourceValidationError",
    "UnmappedField",
    "build_plan",
    "plan_conversion",
    "read_extract",
]
