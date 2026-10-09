"""Command line entry point: python -m loan_lab.scenarios <scenario>

SYNTHETIC, DEMO ONLY. Runs repeatable conversion failure demonstrations, each under a new,
labeled run ID. Inspect the results in the Conversion Management pages (/conversions).
"""

import argparse
import sys
from pathlib import Path

from loan_lab.conversion.legacy.loader import TargetPreconditionError
from loan_lab.paths import CONVERSION_ROOT_RELATIVE, EVIDENCE_ROOT_RELATIVE
from loan_lab.scenarios.conversion import (
    EXPECTED,
    LABEL,
    RUN_ID_PREFIXES,
    SCENARIO_ROOT_RELATIVE,
    TITLES,
    Scenario,
    ScenarioRefusedError,
    ScenarioResult,
    run_scenario,
)

EXIT_AS_EXPECTED = 0
EXIT_UNEXPECTED = 1
EXIT_REFUSED = 3
ALL = "all"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m loan_lab.scenarios",
        description=(
            f"{LABEL}. Runs a repeatable conversion failure demonstration on the synthetic "
            "sample extract, under a new run ID, and checks the run's evidence against the "
            "expected outcome."
        ),
    )
    parser.add_argument(
        "scenario",
        choices=[*Scenario, ALL],
        help="; ".join(f"{s} = {TITLES[s]}" for s in Scenario) + f"; {ALL} = each in turn",
    )
    parser.add_argument(
        "--run-id",
        help="new run ID with the scenario's prefix ("
        + ", ".join(f"{p}..." for p in RUN_ID_PREFIXES.values())
        + "); default: prefix plus UTC timestamp and random suffix. Not allowed with 'all'.",
    )
    parser.add_argument(
        "--conversion-root", type=Path,
        help=f"default: <project root>/{CONVERSION_ROOT_RELATIVE.as_posix()}",
    )
    parser.add_argument(
        "--evidence-root", type=Path,
        help=f"default: <project root>/{EVIDENCE_ROOT_RELATIVE.as_posix()}",
    )
    parser.add_argument(
        "--scenario-root", type=Path,
        help=f"scenario descriptors and generated extracts (default: <project root>/"
        f"{SCENARIO_ROOT_RELATIVE.as_posix()})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.scenario == ALL and args.run_id:
        parser.error("--run-id cannot be used with 'all'")
    scenarios = list(Scenario) if args.scenario == ALL else [Scenario(args.scenario)]

    print(f"*** {LABEL} ***")
    exit_code = EXIT_AS_EXPECTED
    for scenario in scenarios:
        try:
            result = run_scenario(
                scenario, args.run_id,
                conversion_root=args.conversion_root,
                evidence_root=args.evidence_root,
                scenario_root=args.scenario_root,
            )
        except (ScenarioRefusedError, TargetPreconditionError) as error:
            print(f"Scenario {scenario} refused; nothing was run: {error}", file=sys.stderr)
            return EXIT_REFUSED
        _print(result)
        if not result.as_expected:
            exit_code = EXIT_UNEXPECTED
    return exit_code


def _print(result: ScenarioResult) -> None:
    print()
    print(f"Scenario {result.scenario}: {TITLES[result.scenario]}")
    print(f"  Run ID:   {result.run_id}")
    print(f"  Expected: {EXPECTED[result.scenario]}")
    for finding in result.findings:
        print(f"  - {finding}")
    print(f"  Evidence: {result.evidence_directory}")
    print(f"  Scenario descriptor: {result.scenario_directory}")
    print(f"  Inspect:  /conversions/{result.run_id}")
    if result.as_expected:
        print("  Outcome matches the scenario's expectation.")
    else:
        print("  UNEXPECTED OUTCOME:", file=sys.stderr)
        for deviation in result.deviations:
            print(f"    {deviation}", file=sys.stderr)
