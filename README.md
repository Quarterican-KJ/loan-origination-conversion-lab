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
- No authentication, editing, workflow transitions, or conversion yet.

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
| `reports\load_result.json` | Written once a load was attempted: counts and requested amount read back from the database (amounts as decimal strings), transaction states, and failure details |
| `reports\reconciliation.json` | Written by reconciliation: the result of each rule RC-01 to RC-10, totals, distributions, relationships, customers without converted applications, every discrepancy with its source line, target ID, and expected and actual values, and the attempt ID the manifest must match |

The run folder is reserved before the source is validated, so an invalid extract still leaves a
`FAILED` manifest and its archived files, but no database. Run IDs are never reused, even after a
failure. If a source file changes between planning and loading, the run fails with RUN-08 and
nothing is loaded.

If the load commits but its evidence cannot be completed, the run stays `LOADING`, is not ready for
reconciliation, and the command exits with code 6. `recover_run` then inspects the database
read-only and marks the run `LOADED`, `FAILED`, or `UNKNOWN` (when the outcome cannot be verified);
`fail_run` formally fails an `UNKNOWN` run, and `check_ready` reports whether a run may proceed.
None of them reloads data or modifies a database.

Then reconcile the loaded run against its archived source:

```powershell
python -m loan_lab.conversion.legacy.reconcile_cli manual-check-1
```

Reconciliation re-reads the archived files, recomputes every expected value independently (never
reusing the converter's mapped values), and compares every loaded record and relationship with the
database, which it opens read-only. If every rule passes, the run becomes `RECONCILED` and awaits a
release decision; otherwise it becomes `FAILED` and cannot be released. It refuses a run that is
not ready, or whose archived files or database no longer match their recorded checksums. It never
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

## Run the tests

```powershell
python -m pytest
```

## Status

Implemented: the `GET /health` endpoint, the LOS data model (borrowers, applications, parties,
collateral, pledges, liens), the synthetic data generator, a read-only LOS web interface, and
legacy conversion validation, mapping, transactional loading with per-run evidence, and
independent reconciliation. Not yet implemented: authentication, editing, database migrations,
workflow validation, and release approval. See
[docs/architecture.md](docs/architecture.md) for the design and its current limitations.
