from enum import StrEnum


class BorrowerType(StrEnum):
    INDIVIDUAL = "individual"
    BUSINESS = "business"


class LoanProduct(StrEnum):
    CONSUMER_AUTO = "consumer_auto"
    CONSUMER_PERSONAL = "consumer_personal"
    RESIDENTIAL_MORTGAGE = "residential_mortgage"
    HOME_EQUITY = "home_equity"
    COMMERCIAL_TERM = "commercial_term"
    COMMERCIAL_REAL_ESTATE = "commercial_real_estate"


class ApplicationStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


class PartyRole(StrEnum):
    PRIMARY_BORROWER = "primary_borrower"
    CO_BORROWER = "co_borrower"
    GUARANTOR = "guarantor"
