from fastapi import FastAPI

from loan_lab import __version__

app = FastAPI(title="Loan Origination & Data Conversion Lab", version=__version__)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
