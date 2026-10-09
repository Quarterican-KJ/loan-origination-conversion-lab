"""The Legacy LOS CSV data contract (docs/conversion-specification.md), expressed as data.

Nothing here reads files or makes decisions; it only states what the source must look like and
how its codes translate to the target enums.
"""

import re
from enum import StrEnum

from loan_lab.models.enums import ApplicationStatus, BorrowerType, LoanProduct, PartyRole

SOURCE_SYSTEM = "LEGACY_LOS"

BORROWERS_FILE = "borrowers.csv"
APPLICATIONS_FILE = "applications.csv"
PARTIES_FILE = "application_parties.csv"
CONTROL_FILE = "extract_control.csv"
DATA_FILES = (BORROWERS_FILE, APPLICATIONS_FILE, PARTIES_FILE)

HEADERS: dict[str, tuple[str, ...]] = {
    BORROWERS_FILE: (
        "CUST_NO", "CUST_TYPE", "BUSINESS_NAME", "LAST_NAME", "FIRST_NAME", "MIDDLE_INIT",
        "RECORD_STATUS", "LAST_MAINT_DATE",
    ),
    APPLICATIONS_FILE: (
        "APPL_NO", "PROD_CD", "APPL_STAT", "REQ_AMT", "INT_RATE", "TERM_MOS", "APPL_DATE",
        "BRANCH_NO",
    ),
    PARTIES_FILE: ("APPL_NO", "CUST_NO", "REL_CD"),
    CONTROL_FILE: ("FILE_NAME", "RECORD_COUNT", "AMOUNT_TOTAL", "EXTRACT_DATE"),
}

CUST_NO_PATTERN = re.compile(r"[0-9]{8}")
APPL_NO_PATTERN = re.compile(r"[0-9]{10}")
AMOUNT_PATTERN = re.compile(r"[0-9]{1,13}\.[0-9]{2}")
RATE_PATTERN = re.compile(r"[0-9]{6}")
TERM_PATTERN = re.compile(r"[0-9]{1,3}")
COUNT_PATTERN = re.compile(r"[0-9]{1,9}")
DATE_PATTERN = re.compile(r"[0-9]{8}")
BRANCH_PATTERN = re.compile(r"[0-9]{3}")
MIDDLE_INITIAL_PATTERN = re.compile(r"[A-Za-z]")

# Fields with no target column in v1 (spec section 6.1), with their expected shape. They are
# inventoried, never used to accept or reject a record.
UNMAPPED_FIELDS: dict[str, dict[str, re.Pattern[str]]] = {
    BORROWERS_FILE: {"LAST_MAINT_DATE": DATE_PATTERN},
    APPLICATIONS_FILE: {"APPL_DATE": DATE_PATTERN, "BRANCH_NO": BRANCH_PATTERN},
}

MAX_LEGAL_NAME_LENGTH = 200
# Source field limits (SV-11), in Unicode code points after CSV parsing and trimming.
MAX_NAME_LENGTHS: dict[str, int] = {
    "FIRST_NAME": 100,
    "LAST_NAME": 100,
    "BUSINESS_NAME": 200,
}
MIN_TERM_MONTHS = 1
MAX_TERM_MONTHS = 600

# Source code tables (spec section 5). ``None`` means "known to the source, no target mapping".
CUSTOMER_TYPES: dict[str, BorrowerType | None] = {
    "I": BorrowerType.INDIVIDUAL,
    "B": BorrowerType.BUSINESS,
    "T": None,
}
INDIVIDUAL = "I"
RECORD_STATUSES = frozenset({"A", "D"})
DELETED = "D"

PRODUCTS: dict[str, LoanProduct | None] = {
    "110": LoanProduct.CONSUMER_AUTO,
    "120": LoanProduct.CONSUMER_PERSONAL,
    "210": LoanProduct.RESIDENTIAL_MORTGAGE,
    "220": LoanProduct.HOME_EQUITY,
    "310": LoanProduct.COMMERCIAL_TERM,
    "320": LoanProduct.COMMERCIAL_REAL_ESTATE,
    "330": None,
    "900": None,
}
EXCLUDED_PRODUCTS = frozenset({"330", "900"})

STATUSES: dict[str, ApplicationStatus | None] = {
    "P": ApplicationStatus.DRAFT,
    "S": ApplicationStatus.SUBMITTED,
    "U": ApplicationStatus.IN_REVIEW,
    "A": ApplicationStatus.APPROVED,
    "D": ApplicationStatus.DECLINED,
    "W": ApplicationStatus.WITHDRAWN,
    "X": None,
}
VOIDED = "X"

ROLES: dict[str, PartyRole | None] = {
    "PRI": PartyRole.PRIMARY_BORROWER,
    "COB": PartyRole.CO_BORROWER,
    "GTR": PartyRole.GUARANTOR,
    "SGN": None,
}
PRIMARY = "PRI"
SIGNER = "SGN"


class Rule(StrEnum):
    """Stable rule codes from spec section 9 and 10."""

    RUN_01 = "RUN-01"
    RUN_02 = "RUN-02"
    RUN_03 = "RUN-03"
    RUN_04 = "RUN-04"
    RUN_05 = "RUN-05"
    RUN_06 = "RUN-06"

    SV_01 = "SV-01"
    SV_02 = "SV-02"
    SV_03 = "SV-03"
    SV_04 = "SV-04"
    SV_05 = "SV-05"
    SV_06 = "SV-06"
    SV_07 = "SV-07"
    SV_08 = "SV-08"
    SV_09 = "SV-09"
    SV_10 = "SV-10"
    SV_11 = "SV-11"

    MP_01 = "MP-01"
    MP_02 = "MP-02"
    MP_03 = "MP-03"
    MP_04 = "MP-04"

    RF_01 = "RF-01"
    RF_02 = "RF-02"
    RF_03 = "RF-03"
    RF_04 = "RF-04"
    RF_05 = "RF-05"
    RF_06 = "RF-06"
    RF_07 = "RF-07"
    RF_08 = "RF-08"

    EX_01 = "EX-01"
    EX_02 = "EX-02"
    EX_03 = "EX-03"
    EX_04 = "EX-04"
    EX_05 = "EX-05"

    WN_01 = "WN-01"

    @property
    def stage(self) -> str:
        return {
            "RUN": "run",
            "SV": "source_validation",
            "MP": "mapping",
            "RF": "reference",
            "EX": "exclusion",
            "WN": "warning",
        }[self.value.split("-")[0]]


REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    BORROWERS_FILE: ("CUST_NO", "CUST_TYPE", "RECORD_STATUS"),
    APPLICATIONS_FILE: ("APPL_NO", "PROD_CD", "APPL_STAT", "REQ_AMT", "INT_RATE", "TERM_MOS"),
    PARTIES_FILE: ("APPL_NO", "CUST_NO", "REL_CD"),
}
