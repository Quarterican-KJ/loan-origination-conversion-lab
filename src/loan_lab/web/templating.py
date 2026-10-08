from pathlib import Path

from fastapi.templating import Jinja2Templates

from loan_lab.models import ApplicationStatus, LoanProduct
from loan_lab.web.formatting import FILTERS

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"

# Jinja2Templates enables autoescaping for .html templates; never mark user data as safe.
templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.filters.update(FILTERS)
templates.env.globals.update(
    APP_NAME="Loan Origination & Conversion Lab",
    PRODUCTS=list(LoanProduct),
    STATUSES=list(ApplicationStatus),
)
