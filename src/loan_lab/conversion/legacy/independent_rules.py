"""The record rules of docs/conversion-specification.md, restated for independent evaluation.

The independent eligibility evaluator (:mod:`eligibility`) decides what *should* happen to every
source line from these tables, never from the converter's ``contract`` module, so a defect in the
converter's tables cannot reproduce itself in the expected answer (spec 12.1). Each value is
transcribed from the specification section named beside it. This module imports nothing from the
converter.
"""

import re
from types import MappingProxyType

# Section 3 and 4: file names and exact header layouts.
BORROWERS = "borrowers.csv"
APPLICATIONS = "applications.csv"
PARTIES = "application_parties.csv"
DATA_FILES = (BORROWERS, APPLICATIONS, PARTIES)

HEADERS = MappingProxyType(
    {
        BORROWERS: (
            "CUST_NO", "CUST_TYPE", "BUSINESS_NAME", "LAST_NAME", "FIRST_NAME", "MIDDLE_INIT",
            "RECORD_STATUS", "LAST_MAINT_DATE",
        ),
        APPLICATIONS: (
            "APPL_NO", "PROD_CD", "APPL_STAT", "REQ_AMT", "INT_RATE", "TERM_MOS", "APPL_DATE",
            "BRANCH_NO",
        ),
        PARTIES: ("APPL_NO", "CUST_NO", "REL_CD"),
    }
)

# Section 4: columns marked "Required: Yes" (SV-02).
REQUIRED = MappingProxyType(
    {
        BORROWERS: ("CUST_NO", "CUST_TYPE", "RECORD_STATUS"),
        APPLICATIONS: ("APPL_NO", "PROD_CD", "APPL_STAT", "REQ_AMT", "INT_RATE", "TERM_MOS"),
        PARTIES: ("APPL_NO", "CUST_NO", "REL_CD"),
    }
)

# Section 4: formats of non-blank values, with the rule a mismatch fails (SV-03 to SV-06).
_CUST_NO = re.compile(r"[0-9]{8}")
_APPL_NO = re.compile(r"[0-9]{10}")
FORMATS = MappingProxyType(
    {
        BORROWERS: (("CUST_NO", _CUST_NO, "SV-03"),),
        APPLICATIONS: (
            ("APPL_NO", _APPL_NO, "SV-03"),
            ("REQ_AMT", re.compile(r"[0-9]{1,13}\.[0-9]{2}"), "SV-04"),
            ("INT_RATE", re.compile(r"[0-9]{6}"), "SV-05"),
            ("TERM_MOS", re.compile(r"[0-9]{1,3}"), "SV-06"),
        ),
        PARTIES: (("APPL_NO", _APPL_NO, "SV-03"), ("CUST_NO", _CUST_NO, "SV-03")),
    }
)

# Section 4.1: a populated MIDDLE_INIT is one letter (SV-08).
MIDDLE_INITIAL = re.compile(r"[A-Za-z]")

# Section 5: source code tables. A code not in its table is unknown (SV-07); a code mapped to
# None is known to the source but has no target mapping (MP-01) unless an exclusion applies first.
CUSTOMER_TYPES = MappingProxyType({"I": "individual", "B": "business", "T": None})
RECORD_STATUSES = MappingProxyType({"A": "active", "D": "deleted"})
PRODUCTS = MappingProxyType(
    {
        "110": "consumer_auto",
        "120": "consumer_personal",
        "210": "residential_mortgage",
        "220": "home_equity",
        "310": "commercial_term",
        "320": "commercial_real_estate",
        "330": None,
        "900": None,
    }
)
STATUSES = MappingProxyType(
    {
        "P": "draft",
        "S": "submitted",
        "U": "in_review",
        "A": "approved",
        "D": "declined",
        "W": "withdrawn",
        "X": None,
    }
)
ROLES = MappingProxyType(
    {"PRI": "primary_borrower", "COB": "co_borrower", "GTR": "guarantor", "SGN": None}
)

# Code fields by file, with their tables. RECORD_STATUS drives exclusion only, so it is never
# mapped and never fails MP-01 (section 6).
CODE_FIELDS = MappingProxyType(
    {
        BORROWERS: (("CUST_TYPE", CUSTOMER_TYPES), ("RECORD_STATUS", RECORD_STATUSES)),
        APPLICATIONS: (("PROD_CD", PRODUCTS), ("APPL_STAT", STATUSES)),
        PARTIES: (("REL_CD", ROLES),),
    }
)
MAPPED_CODE_FIELDS = MappingProxyType(
    {
        BORROWERS: (("CUST_TYPE", CUSTOMER_TYPES),),
        APPLICATIONS: (("PROD_CD", PRODUCTS), ("APPL_STAT", STATUSES)),
        PARTIES: (("REL_CD", ROLES),),
    }
)

INDIVIDUAL = "I"
PRIMARY = "PRI"

# Section 10: exclusion criteria EX-01 to EX-04, each a single valid code in one field.
EXCLUSIONS = MappingProxyType(
    {
        BORROWERS: (("EX-01", "RECORD_STATUS", frozenset({"D"})),),
        APPLICATIONS: (
            ("EX-02", "PROD_CD", frozenset({"330", "900"})),
            ("EX-03", "APPL_STAT", frozenset({"X"})),
        ),
        PARTIES: (("EX-04", "REL_CD", frozenset({"SGN"})),),
    }
)

# Section 4.1: names required for each customer type, and names that must then be empty (SV-02,
# SV-08). SV-11 applies only to the required ones (section 9.2).
TYPE_REQUIRED_NAMES = MappingProxyType(
    {
        "I": ("LAST_NAME", "FIRST_NAME"),
        "B": ("BUSINESS_NAME",),
        "T": ("BUSINESS_NAME",),
    }
)
TYPE_EMPTY_NAMES = MappingProxyType(
    {
        "I": ("BUSINESS_NAME",),
        "B": ("LAST_NAME", "FIRST_NAME", "MIDDLE_INIT"),
        "T": ("LAST_NAME", "FIRST_NAME", "MIDDLE_INIT"),
    }
)

# Section 9.2 (SV-11) and 9.3 (MP-04): lengths in Unicode code points.
NAME_LIMITS = MappingProxyType({"FIRST_NAME": 100, "LAST_NAME": 100, "BUSINESS_NAME": 200})
LEGAL_NAME_LIMIT = 200

# Section 9.3: MP-03 term bounds, inclusive.
TERM_MONTHS_MIN = 1
TERM_MONTHS_MAX = 600

# Section 13.1: the report stage of each rule family.
STAGES = MappingProxyType(
    {
        "SV": "source_validation",
        "MP": "mapping",
        "RF": "reference",
        "EX": "exclusion",
        "WN": "warning",
    }
)


def stage_of(rule: str) -> str:
    return STAGES[rule.split("-")[0]]
