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
│   └── learning-log.md      # Notes on Python and loan origination concepts
├── src/
│   └── loan_lab/
│       ├── main.py          # FastAPI app (GET /health)
│       ├── models/          # SQLAlchemy models (future)
│       ├── conversion/      # Legacy extract/transform/load (future)
│       ├── validation/      # Data validation rules (future)
│       ├── reconciliation/  # Source-to-target reconciliation (future)
│       └── web/             # Jinja2 web views (future)
└── tests/
    └── test_health.py       # Health endpoint test
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

## Run the app

```powershell
uvicorn loan_lab.main:app --reload
```

Then open:

- Health check: <http://127.0.0.1:8000/health> → `{"status":"ok"}`
- Interactive API docs: <http://127.0.0.1:8000/docs>

Stop the server with `Ctrl+C`.

## Run the tests

```powershell
python -m pytest
```

## Status

Scaffold only. Loan business logic, database tables, and UI have not been implemented yet. See [docs/architecture.md](docs/architecture.md) for the planned design.
