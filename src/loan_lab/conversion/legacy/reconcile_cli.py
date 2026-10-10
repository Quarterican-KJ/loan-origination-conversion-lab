"""Command line entry point: python -m loan_lab.conversion.legacy.reconcile_cli <run_id>

Reconciles a LOADED run against its archived source and writes reports/reconciliation.json.
It never writes to the conversion database and never releases or declines a run.
"""

import argparse
import sys
from pathlib import Path

from loan_lab.conversion.legacy.reconcile import (
    ReconciliationConflictError,
    ReconciliationNotFinalizedError,
    ReconciliationRefusedError,
    reconcile_run,
)
from loan_lab.paths import EVIDENCE_ROOT_RELATIVE

EXIT_RECONCILED = 0
EXIT_MISMATCH = 7
EXIT_REFUSED = 8
EXIT_CONFLICT = 9
EXIT_NOT_FINALIZED = 10


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m loan_lab.conversion.legacy.reconcile_cli",
        description="Reconcile a loaded conversion run against its archived source extract.",
    )
    parser.add_argument("run_id", help="the LOADED run to reconcile")
    parser.add_argument(
        "--evidence-root",
        type=Path,
        help=f"parent of the run evidence directories (default: <project root>/"
        f"{EVIDENCE_ROOT_RELATIVE.as_posix()})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = reconcile_run(args.run_id, evidence_root=args.evidence_root)
    except ReconciliationRefusedError as error:
        print(f"Reconciliation of run {args.run_id} refused; nothing was written.", file=sys.stderr)
        for problem in error.problems:
            print(f"  {problem}", file=sys.stderr)
        return EXIT_REFUSED
    except ReconciliationConflictError as error:
        print(
            f"Reconciliation evidence for run {args.run_id} is in conflict and was preserved. "
            "The run is not reconciled; investigate, then fail it formally.",
            file=sys.stderr,
        )
        for problem in error.problems:
            print(f"  {problem}", file=sys.stderr)
        return EXIT_CONFLICT
    except ReconciliationNotFinalizedError as error:
        print(str(error), file=sys.stderr)
        return EXIT_NOT_FINALIZED

    stream = sys.stdout if result.passed else sys.stderr
    print(f"Run {result.run_id} {result.status}. Report: {result.report_path}", file=stream)
    for rule in result.rules:
        verdict = "PASS" if rule.passed else (
            f"FAIL ({rule.discrepancies})" if rule.discrepancies else "INCOMPLETE"
        )
        if not rule.complete:
            verdict += f", {(rule.not_evaluated or {}).get('lines')} lines not compared"
        print(f"  {rule.rule} {rule.title:<48}{verdict}", file=stream)
    for discrepancy in result.discrepancies[:20]:
        where = f"{discrepancy.file}:{discrepancy.line}" if discrepancy.line else (
            discrepancy.file or discrepancy.target_table or ""
        )
        print(
            f"  {discrepancy.rule} {where} {discrepancy.source_key or ''} "
            f"{discrepancy.field or ''}: expected {discrepancy.expected}, "
            f"actual {discrepancy.actual}. {discrepancy.message}",
            file=stream,
        )
    if len(result.discrepancies) > 20:
        print(f"  ... {len(result.discrepancies) - 20} more in the report.", file=stream)
    if result.passed:
        print("  Awaiting release approval; the run has not been released.")
        return EXIT_RECONCILED
    print("  The run is FAILED and cannot be released.", file=stream)
    return EXIT_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
