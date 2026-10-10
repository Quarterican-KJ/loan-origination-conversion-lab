import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi.templating import Jinja2Templates

from loan_lab.conversion.legacy.contract import DATA_FILES
from loan_lab.models import ApplicationStatus, LoanProduct
from loan_lab.web.formatting import FILTERS, TESTS

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"


@lru_cache(maxsize=32)
def _content_hash(path: str, mtime_ns: int, size: int) -> str:
    return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:12]


def asset_version(path: str) -> str:
    """Short hash of a static file's content, so an edited file is fetched under a new URL."""
    stat = (STATIC_DIR / path).stat()
    return _content_hash(path, stat.st_mtime_ns, stat.st_size)


# Jinja2Templates enables autoescaping for .html templates; never mark user data as safe.
templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.filters.update(FILTERS)
templates.env.tests.update(TESTS)
templates.env.globals.update(
    APP_NAME="Loan Origination & Conversion Lab",
    PRODUCTS=list(LoanProduct),
    STATUSES=list(ApplicationStatus),
    CONVERSION_DATA_FILES=list(DATA_FILES),
    asset_version=asset_version,
)
