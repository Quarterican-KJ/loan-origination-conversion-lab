"""Synthetic, demo-only conversion failure scenarios (Milestone 8).

Nothing in ``loan_lab.conversion`` imports this package, and the conversion commands offer none
of its options. Defect injection exists only here, behind ``python -m loan_lab.scenarios``.
"""

from loan_lab.scenarios.conversion import (
    RATE_DEFECT,
    RUN_ID_PREFIXES,
    InjectedRateDefect,
    Scenario,
    ScenarioRefusedError,
    ScenarioResult,
    default_scenario_root,
    inject_rate_defect,
    new_scenario_run_id,
    run_scenario,
)

__all__ = [
    "RATE_DEFECT",
    "RUN_ID_PREFIXES",
    "InjectedRateDefect",
    "Scenario",
    "ScenarioRefusedError",
    "ScenarioResult",
    "default_scenario_root",
    "inject_rate_defect",
    "new_scenario_run_id",
    "run_scenario",
]
