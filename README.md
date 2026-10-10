# Loan Origination & Data Conversion Lab

A Python learning lab that simulates two things found in banking technology:

1. **A mock Loan Origination System (LOS)** — where loan applications are captured and tracked.
2. **A legacy loan-data conversion engine** — which extracts loan records from an older "legacy" format, transforms and validates them, loads them into the LOS, and reconciles the results.

> **Synthetic data only.** This project contains no real customer data and no proprietary vendor code, schemas, or branding. All sample data is fabricated for learning purposes.

## Stack

- Python 3.14
- FastAPI (web/API framework) + Uvicorn (ASGI server)
- SQLAlchemy 2.x with SQLite
- Jinja2 (server-rendered HTML templates)
- pytest (testing)

## Project layout

```
.
├── pyproject.toml           # Project metadata and dependencies
├── .env.example             # Template for local environment settings
├── docs/
│   ├── architecture.md      # System design: mock LOS + conversion engine
│   ├── conversion-specification.md  # Legacy CSV conversion data contract (draft)
│   └── learning-log.md      # Notes on Python and loan origination concepts
├── data/                    # Local SQLite databases (git-ignored, created on demand)
├── sample_data/legacy/      # Small synthetic legacy CSV extract for the conversion spec
├── src/
│   └── loan_lab/
│       ├── main.py          # FastAPI app factory (GET /health + web interface)
│       ├── db.py            # SQLAlchemy engine setup (SQLite foreign keys on)
│       ├── paths.py         # Project-root and default database path resolution
│       ├── models/          # SQLAlchemy models: borrowers, applications, collateral, liens
│       ├── synthetic/       # Deterministic synthetic data generator + seeding CLI
│       ├── conversion/      # Legacy CSV conversion: validate, map, load, and reconcile
│       ├── scenarios/       # Synthetic, demo-only conversion failure scenarios
│       ├── validation/      # Data validation rules (future)
│       ├── reconciliation/  # Source-to-target reconciliation (future)
│       └── web/             # Read-only LOS interface: routes, queries, templates, static CSS/JS
└── tests/                   # pytest suite (in-memory / temporary databases only)
```

## Setup (Windows PowerShell)

From the repository root:

```powershell
# 1. Create the virtual environment (skip if .venv already exists)
py -3.14 -m venv .venv

# 2. Activate it
.\.venv\Scripts\Activate.ps1
# If activation is blocked, allow local scripts for your user once:
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# 3. Upgrade pip and install the project in editable mode with dev tools
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

# 4. (Optional) Create your local environment file
Copy-Item .env.example .env
```

## Run the LOS interface

The web interface reads the development database `data\loan_lab_dev.db`. Seed it first
(see [Generate synthetic data](#generate-synthetic-data)), then start the server:

```powershell
python -m loan_lab.synthetic --preset demo   # only needed once
python -m uvicorn loan_lab.main:app --reload
```

Then open:

| Page | URL |
| --- | --- |
| Dashboard: counts, requested volume, product and status breakdowns | <http://127.0.0.1:8000/> |
| Application directory: search, product/status filters, pagination | <http://127.0.0.1:8000/applications> |
| Application detail: terms, parties, collateral, appraisals, liens | <http://127.0.0.1:8000/applications/1> |
| Conversion runs found in `output\conversion` | <http://127.0.0.1:8000/conversions> |
| Conversion run summary: status, source checksums, row dispositions, totals, warnings | <http://127.0.0.1:8000/conversions/manual-check-1> |
| Reconciliation: RC-01 to RC-12 (RC-01 to RC-10 for runs reconciled before Milestone 10) and field-level discrepancies | <http://127.0.0.1:8000/conversions/manual-check-1/reconciliation> |
| Exception, exclusion, and warning reports: search, file/rule/dependent filters, CSV download | <http://127.0.0.1:8000/conversions/manual-check-1/reports/exceptions> |
| Health check → `{"status":"ok"}` | <http://127.0.0.1:8000/health> |
| Interactive API docs | <http://127.0.0.1:8000/docs> |

Stop the server with `Ctrl+C`.

Notes:

- **Read-only.** The interface opens SQLite in read-only mode and only serves `GET` routes.
  It never creates, seeds, or modifies the database; other methods return `405`. If the
  database is missing or empty, pages show a `503` message with the seeding command.
- **Search** matches any party's name on an application (borrower, co-borrower, or guarantor)
  case-insensitively, or an application ID such as `42` or `#42`.
- **Theme.** Dark by default; use the sun/moon button in the top bar to switch. The choice is
  saved in the browser's local storage.
- Collateral without an appraisal shows its value as **Unknown**, never `$0`.
- **Conversion Management** reads each run's `manifest.json`, `reports\load_result.json`,
  `reports\reconciliation.json`, and the three record reports (see [Convert the sample legacy extract](#convert-the-sample-legacy-extract)).
  A report is shown or downloaded only if it matches the checksum and row count in the manifest.
  Values are shown exactly as extracted; the download prefixes values a spreadsheet could run as
  a formula (starting with `=`, `+`, `-`, `@`) with `'`, while the archived report is unchanged.
  It never opens a conversion database or `loan_lab_dev.db` and never writes evidence. Missing
  evidence shows as **Not available**, never zero or PASS. Each run shows its recorded status
  and, separately, whether the evidence is trusted. Incomplete, conflicting, or unverified
  evidence is labeled **Untrusted evidence**, whatever the recorded status. A reconciled run is
  shown as "Reconciled · awaiting release approval", never as released.
- No authentication, editing, workflow transitions, approvals, or conversion execution from the
  browser yet.

## Generate synthetic data

The generator fills a dedicated SQLite development database, `data\loan_lab_dev.db` in the
project root, with fictional borrowers, applications, parties, collateral, pledges, and liens.
The default location is the same whichever folder you run the command from. It never runs on
application startup and never touches the test suite's databases.

| Preset | Loan applications | Typical time |
| --- | --- | --- |
| `small` | 25 | under 1 second |
| `demo` | 5,000 | about 1 second |
| `stress` | 100,000 | not yet measured |

```powershell
# Seed a fresh development database (creates data\loan_lab_dev.db)
python -m loan_lab.synthetic --preset small

# Larger datasets
python -m loan_lab.synthetic --preset demo
python -m loan_lab.synthetic --preset stress

# Use a different database file or seed. An explicit --database path is used as given;
# relative paths are relative to the current folder.
python -m loan_lab.synthetic --preset small --database data\scratch.db --seed 42

# Show all options
python -m loan_lab.synthetic --help
```

The same preset and seed always produce the same data. Every run prints counts for each entity
type, read back from the database.

### Reseeding an existing database

The generator refuses to write to a database that already contains LOS data. To replace it,
pass `--reset`. You then have to confirm a second time by typing the database file name:

```powershell
python -m loan_lab.synthetic --preset demo --reset
# This permanently deletes 28,941 LOS rows from ...\data\loan_lab_dev.db.
# Type the database file name (loan_lab_dev.db) to confirm: loan_lab_dev.db
```

For scripts or other non-interactive use, pass the confirmation explicitly:

```powershell
python -m loan_lab.synthetic --preset demo --reset --confirm-reset loan_lab_dev.db
```

Without a matching confirmation nothing is changed. The generator also refuses any database that
contains tables it does not manage. To start over completely, delete the file from the
repository root:

```powershell
Remove-Item data\loan_lab_dev.db
```

## Convert the sample legacy extract

Validates `sample_data\legacy`, then loads the eligible records into a new, isolated database at
`data\conversion\<run_id>\loan_lab_conversion.db`. It never touches `data\loan_lab_dev.db`, and it
refuses to reuse an existing run ID. There is no overwrite option.

```powershell
python -m loan_lab.conversion.legacy sample_data\legacy --run-id manual-check-1
```

Each run keeps its evidence in `output\conversion\<run_id>\` (git-ignored):

| Path | Contents |
| --- | --- |
| `manifest.json` | Run ID, UTC timestamps, status (`STARTED`, `VALIDATED`, `LOADING`, `LOADED`, `RECONCILED`, `FAILED`, or `UNKNOWN`), the database transaction state and evidence state (recorded separately), whether the run is ready for reconciliation, source checksums, validation counts or run-level errors, expected target counts and amount, database checksum, and the failure stage, step, and reason |
| `source\` | Exact copies of the source files that could be read, checked against the checksums taken at planning |
| `reports\exceptions.csv` | Every rejected source row, one row per rule failure, including rows rejected only because their application unit was (`DEPENDENT` = `Y`), with file, line, source key, unit key, rule, root cause, field, exact source value, message, remediation, and the raw source line. Sample extract: 18 rows. |
| `reports\exclusions.csv` | Deliberate exclusions (EX-01 to EX-05), with the same columns; dependent EX-05 rows point to their application's exclusion. Sample extract: 6 rows. |
| `reports\warnings.csv` | Nonblocking warnings: customers that load without any converted application (WN-01). Sample extract: 3 rows. |
| `reports\load_result.json` | Written once a load was attempted: counts and requested amount read back from the database (amounts as decimal strings), transaction states, and failure details |
| `reports\reconciliation.json` | Written by reconciliation (`report_version` 2): the result of each rule RC-01 to RC-12, the independently determined eligibility counts, totals, distributions, relationships, customers without converted applications, every discrepancy with its source line, target ID, and expected and actual values, and the attempt ID the manifest must match |

The run folder is reserved before the source is validated, so an invalid extract still leaves a
`FAILED` manifest and its archived files, but no database. Run IDs are never reused, even after a
failure. If a source file changes between planning and loading, the run fails with RUN-08 and
nothing is loaded.

The three record reports are written from the validated plan before anything is loaded. They are
checked against the plan's row counts first, written atomically, and recorded in the manifest
with their SHA-256 checksums and counts. If any of them cannot be written or verified, the run
fails at stage `reports` with exit code 11, no database is created, and any files already written
are kept. A run is ready for reconciliation only while its reports still match the manifest. Runs
created before the reports existed (such as `DEMO-001`) have none and are not changed.

If the load commits but its evidence cannot be completed, the run stays `LOADING`, is not ready for
reconciliation, and the command exits with code 6. `recover_run` then inspects the database
read-only and marks the run `LOADED`, `FAILED`, or `UNKNOWN` (when the outcome cannot be verified);
`fail_run` formally fails an `UNKNOWN` run, and `check_ready` reports whether a run may proceed.
None of them reloads data or modifies a database.

Then reconcile the loaded run against its archived source:

```powershell
python -m loan_lab.conversion.legacy.reconcile_cli manual-check-1
```

Reconciliation re-reads the archived files, decides each source line's eligibility and recomputes
every expected value independently (never running the converter's planner or reusing its mapped
values), and compares every loaded record and relationship with the database, which it opens
read-only. It also compares each line's disposition and target membership (RC-11) and the rows of
the exception, exclusion, and warning reports (RC-12) with that independent answer. If every rule
passes, the run becomes `RECONCILED` and awaits a release decision; otherwise it becomes `FAILED`
and cannot be released. It refuses a run that is not ready, whose archived files or database no
longer match their recorded checksums, or that is `LOADED` but predates the record reports. Runs
reconciled before Milestone 10 (report version 1) keep their reconciliation unchanged. It never
modifies the database, reloads, or releases a run; release approval is not implemented yet. See
the manual verification procedure in [docs/architecture.md](docs/architecture.md).

The manifest records each reconciliation attempt before the report is written, and the run counts
as reconciled only once the manifest is finalized with the report's checksum. If finalization
fails (exit code 10), the run stays `LOADED` and not ready, with the report kept. Running the
command again re-checks the source and database checksums and the report, then finalizes it.
Contradictory reconciliation evidence (exit code 9) is preserved, never overwritten; investigate,
then close the run with `fail_run`. `verify_reconciliation` re-checks a `RECONCILED` run's
evidence later. The recovery procedure and the remaining independence limits are in spec
section 12 and [docs/architecture.md](docs/architecture.md).

## Demonstrate conversion failures (synthetic, demo only)

Three repeatable scenarios run the real conversion pipeline on the synthetic sample extract and
show how each kind of failure is caught. Each run gets a new, labeled run ID and its own
evidence, so you can run them again at any time.

```powershell
python -m loan_lab.scenarios all              # or one of the scenarios below
python -m loan_lab.scenarios loader-defect
```

| Scenario | Run ID prefix | What it does | Expected outcome |
| --- | --- | --- | --- |
| `control-totals` | `SYN-CTRL-` | Copies the sample extract, then misstates the applications row of `extract_control.csv`: `RECORD_COUNT` 13 → 14 and `AMOUNT_TOTAL` 5467000.00 → 5476000.00 | `FAILED` at validation with RUN-04 and RUN-05. All four source files are archived with matching checksums. No target database or load report exists. |
| `business-rejections` | `SYN-REJ-` | Converts the sample extract unchanged. Among other rejections, `0000500107` has an invalid amount (MP-02), `0000500113` has no primary borrower (RF-06), and a party row references a missing application (RF-01). | Rows (read / loaded / excluded / rejected): borrowers 16/11/1/4, applications 13/6/2/5, parties 20/10/3/7. REQ_AMT loaded 2,281,000.00, excluded 291,000.00, rejected 2,895,000.00. Then `RECONCILED`, with all ten RC rules passing. |
| `loader-defect` | `SYN-DEFECT-` | Plans the sample extract normally, then changes the interest rate of the first eligible application (`0000500101`) from 6.5000 to 6.6000 in the load input. The transactional loader writes it, and the database checksum is recorded afterwards. | Loaded counts (11 / 6 / 10) and requested dollars (2,281,000.00) are unchanged. Reconciliation then reports one RC-07 discrepancy (`interest_rate` expected 6.5000, actual 6.6000) and marks the run `FAILED`. |

The command checks each run's evidence against these expectations and prints what it found. It
exits 0 when every scenario behaved as expected, 1 if any did not, and 3 if a run ID was refused
(wrong prefix or already used). Use `--run-id SYN-DEFECT-my-demo` to choose an ID for a single
scenario. `--evidence-root`, `--conversion-root`, and `--scenario-root` work as for the
conversion command.

To inspect a scenario, start the web interface and open `/conversions`. The runs appear
alongside any others. The pages read each run's actual evidence:
- `/conversions/<run_id>` shows the RUN-04 and RUN-05 issues for `control-totals`, and the
  rejections and exclusions for `business-rejections`.
- `/conversions/<run_id>/reconciliation` shows the RC-07 discrepancy for `loader-defect`.

The files are in `output\conversion\<run_id>\` (evidence), `data\conversion\<run_id>\`
(database), and `data\scenarios\<run_id>\`. That last folder holds `scenario.json` and, for
`control-totals`, the altered extract. `scenario.json` labels the run as synthetic and
demo-only and records exactly what was altered. All three locations are git-ignored. Delete a
scenario's three folders by hand when you no longer need it.

Safeguards:
- Defect injection exists only in `loan_lab.scenarios`. The conversion and reconciliation
  commands have no such option, and nothing in `loan_lab.conversion` imports the scenarios.
- The conversion rules, loader, and evidence code are not modified.
- Run IDs must carry the scenario's prefix, and an ID already used by any run is refused before
  anything is written.
- `DEMO-001`, `data\loan_lab_dev.db`, and `sample_data\legacy` are never written.

## Run the tests

```powershell
python -m pytest
```

## Status

Implemented: the `GET /health` endpoint, the LOS data model (borrowers, applications, parties,
collateral, pledges, liens), the synthetic data generator, a read-only LOS web interface with
read-only Conversion Management pages, and legacy conversion validation, mapping, transactional
loading with per-run evidence, independent reconciliation, and synthetic conversion failure
scenarios. Not yet implemented: authentication, editing, database migrations,
workflow validation, and release approval. See
[docs/architecture.md](docs/architecture.md) for the design and its current limitations.

Known issues and planned work are tracked in [docs/backlog.md](docs/backlog.md). Log an issue
when you find it, classify it as P1, P2, or P3, and define its acceptance criteria. Resolve P1
issues before publishing, and update an issue's status once its fix is tested and committed.
