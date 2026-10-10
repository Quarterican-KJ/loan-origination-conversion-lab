"""Command line entry point: python -m loan_lab.conversion.legacy sample_data/legacy

Reserves output/conversion/<run_id>/, archives the extract there, validates and plans it, and
loads it into a new database under data/conversion/<run_id>/, recording the run's evidence
(manifest, archived source, load report) as it goes.
Reconciliation is a separate step (python -m loan_lab.conversion.legacy.reconcile_cli <run_id>).
Release approval is not implemented yet.
"""

import argparse
import sys
from pathlib import Path

from loan_lab.conversion.legacy import contract
from loan_lab.conversion.legacy.contract import Rule
from loan_lab.conversion.legacy.loader import (
    DEFAULT_LOAD_BATCH_SIZE,
    TargetPreconditionError,
    new_run_id,
)
from loan_lab.conversion.legacy.plan import Disposition
from loan_lab.conversion.legacy.reports import REPORT_KINDS
from loan_lab.conversion.legacy.run import (
    REPORTS_DIRECTORY,
    EvidenceIncompleteError,
    LoadRunFailedError,
    ReportRunFailedError,
    SourceRunFailedError,
    run_conversion,
)
from loan_lab.paths import CONVERSION_ROOT_RELATIVE, EVIDENCE_ROOT_RELATIVE

EXIT_OK = 0
EXIT_SOURCE_INVALID = 2
EXIT_TARGET_NOT_NEW = 3
EXIT_LOAD_FAILED = 4
EXIT_SOURCE_CHANGED = 5
EXIT_EVIDENCE_INCOMPLETE = 6
EXIT_REPORTS_FAILED = 11

LABELS = {
    contract.BORROWERS_FILE: "Borrowers",
    contract.APPLICATIONS_FILE: "Applications",
    contract.PARTIES_FILE: "Parties",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m loan_lab.conversion.legacy",
        description="Validate a legacy LOS extract and load it into a new conversion database.",
    )
    parser.add_argument("source", type=Path, help="directory holding the four extract files")
    parser.add_argument("--run-id", help="new run ID (default: UTC timestamp plus random suffix)")
    parser.add_argument(
        "--conversion-root",
        type=Path,
        help=f"parent of the run databases (default: <project root>/"
        f"{CONVERSION_ROOT_RELATIVE.as_posix()})",
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        help=f"parent of the run evidence directories (default: <project root>/"
        f"{EVIDENCE_ROOT_RELATIVE.as_posix()})",
    )
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=DEFAULT_LOAD_BATCH_SIZE,
        help="rows per INSERT batch inside the single load transaction",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_id = args.run_id or new_run_id()

    try:
        run = run_conversion(
            args.source,
            run_id,
            conversion_root=args.conversion_root,
            evidence_root=args.evidence_root,
            batch_size=args.batch_size,
        )
    except TargetPreconditionError as error:
        print(f"Run {run_id} refused; nothing was written: {error.issue}", file=sys.stderr)
        return EXIT_TARGET_NOT_NEW
    except SourceRunFailedError as error:
        print(
            f"Run {run_id} FAILED at stage validation; nothing was loaded. "
            f"Evidence: {error.evidence_directory}",
            file=sys.stderr,
        )
        for issue in error.issues:
            print(f"  {issue}", file=sys.stderr)
        return EXIT_SOURCE_INVALID
    except ReportRunFailedError as error:
        print(str(error), file=sys.stderr)
        return EXIT_REPORTS_FAILED
    except LoadRunFailedError as error:
        print(str(error), file=sys.stderr)
        return {
            Rule.RUN_07: EXIT_TARGET_NOT_NEW, Rule.RUN_08: EXIT_SOURCE_CHANGED
        }.get(error.rule, EXIT_LOAD_FAILED)  # type: ignore[arg-type]
    except EvidenceIncompleteError as error:
        print(str(error), file=sys.stderr)
        return EXIT_EVIDENCE_INCOMPLETE

    plan = run.plan
    result = run.result
    loaded = {
        contract.BORROWERS_FILE: result.counts.borrowers,
        contract.APPLICATIONS_FILE: result.counts.applications,
        contract.PARTIES_FILE: result.counts.parties,
    }
    print(f"Run {run_id} LOADED into {result.database_path}")
    print(f"  Evidence: {run.evidence_directory}")
    print(f"  {'':<14}{'Read':>8}{'Loaded':>8}{'Excluded':>10}{'Rejected':>10}")
    for file, label in LABELS.items():
        counts = plan.disposition_counts(file)
        print(
            f"  {label:<14}{len(plan.rows(file)):>8}{loaded[file]:>8}"
            f"{counts[Disposition.EXCLUDED]:>10}{counts[Disposition.REJECTED]:>10}"
        )
    print(f"  Requested amount loaded: {run.database.requested_amount}")
    standalone = ", ".join(result.borrowers_without_relationships) or "none"
    print(f"  Customers without a converted application (WN-01): {standalone}")
    reports = ", ".join(f"{REPORTS_DIRECTORY}/{kind.file_name}" for kind in REPORT_KINDS)
    print(f"  Reports: {reports}")
    print("  Reconciliation has not run; this run is not releasable. Reconcile it with:")
    print(f"  python -m loan_lab.conversion.legacy.reconcile_cli {run_id}")
    return EXIT_OK


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number
