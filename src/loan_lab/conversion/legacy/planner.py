"""Build a :class:`ConversionPlan` from a structurally valid extract (spec sections 8 to 10).

Evaluation follows spec section 9.6. Every decision is derived from the contract tables and the
source rows; nothing here knows the expected outcome of any particular record.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

from loan_lab.conversion.legacy import contract
from loan_lab.conversion.legacy.contract import Rule
from loan_lab.conversion.legacy.plan import (
    AmountTotals,
    ApplicationUnit,
    Cause,
    ConversionPlan,
    Disposition,
    Issue,
    MappedApplication,
    MappedBorrower,
    MappedParty,
    MappedRecord,
    RowResult,
    UnmappedField,
)
from loan_lab.conversion.legacy.source import SourceExtract, SourceRef, SourceRow, read_extract
from loan_lab.conversion.legacy.transforms import (
    business_name,
    exact_sum,
    individual_name,
    parse_amount,
    parse_rate,
    parse_term,
)


def plan_conversion(directory: Path) -> ConversionPlan:
    """Read, validate, and map an extract. Raises ``SourceValidationError`` on run-level failure."""
    return build_plan(read_extract(directory))


def build_plan(extract: SourceExtract) -> ConversionPlan:
    borrowers = [_Draft(row, _key(row, "CUST_NO")) for row in extract.borrowers.rows]
    applications = [_Draft(row, _key(row, "APPL_NO")) for row in extract.applications.rows]
    parties = [_Draft(row, _key(row, "APPL_NO", "CUST_NO")) for row in extract.parties.rows]

    # 2. Structure.
    for draft in (*borrowers, *applications, *parties):
        _check_structure(draft)

    # 3. Duplicate keys, over every readable row including ones that would be excluded.
    _check_duplicates(borrowers, ("CUST_NO",), Rule.SV_09)
    _check_duplicates(applications, ("APPL_NO",), Rule.SV_09)
    _check_duplicates(parties, ("APPL_NO", "CUST_NO"), Rule.SV_10)

    # 4. Exclusions. Excluded rows are not validated further.
    for draft in borrowers:
        _exclude_borrower(draft)
    for draft in applications:
        _exclude_application(draft)
    applications_by_key = _index(applications, "APPL_NO")
    for draft in parties:
        _exclude_party(draft, applications_by_key)

    # 5-6. Remaining source checks, then mapping, on rows still in play.
    for draft in borrowers:
        _validate_borrower(draft)
        _map_borrower(draft)
    for draft in applications:
        _validate_application(draft)
        _map_application(draft)
    for draft in parties:
        _validate_party(draft)
        _map_party(draft)

    # 7-9. References, conversion units, and dependent outcomes.
    borrowers_by_key = _index(borrowers, "CUST_NO")
    _resolve_references(parties, applications_by_key, borrowers_by_key)
    unit_drafts = _resolve_units(applications_by_key, parties)

    # 10. Warnings.
    _warn_customers_without_applications(borrowers, parties)

    frozen = {id(draft): draft.freeze() for draft in (*borrowers, *applications, *parties)}
    units = tuple(
        ApplicationUnit(
            key=key,
            applications=tuple(frozen[id(d)] for d in app_drafts),
            parties=tuple(frozen[id(d)] for d in party_drafts),
            disposition=_unit_disposition(app_drafts),
        )
        for key, app_drafts, party_drafts in unit_drafts
    )
    return ConversionPlan(
        control=extract.control,
        checksums=extract.checksums,
        borrowers=tuple(frozen[id(d)] for d in borrowers),
        applications=tuple(frozen[id(d)] for d in applications),
        parties=tuple(frozen[id(d)] for d in parties),
        units=units,
        amounts=_amount_totals(applications),
        unmapped_fields=_unmapped_inventory(
            {contract.BORROWERS_FILE: borrowers, contract.APPLICATIONS_FILE: applications}
        ),
    )


class _Draft:
    """Mutable working state for one source row while the plan is being built."""

    def __init__(self, row: SourceRow, key: str) -> None:
        self.row = row
        self.key = key
        self.issues: list[Issue] = []
        self.exclusions: list[Issue] = []
        self.warnings: list[Issue] = []
        self.dependent = False
        self.unit_key: str | None = None
        self.target: MappedRecord | None = None

    @property
    def ref(self) -> SourceRef:
        return self.row.ref

    @property
    def open(self) -> bool:
        """Neither rejected nor excluded so far."""
        return not self.issues and not self.exclusions

    @property
    def excluded(self) -> bool:
        return bool(self.exclusions) and not self.issues

    def value(self, name: str) -> str:
        return self.row.value(name)

    def reject(
        self,
        rule: Rule,
        message: str,
        field: str | None = None,
        causes: Iterable[Cause] | None = None,
    ) -> None:
        value = self.value(field) if field and self.row.well_formed else None
        self.issues.append(Issue(rule, message, field, value, self._causes(rule, causes)))

    def exclude(self, rule: Rule, message: str, field: str | None = None,
                causes: Iterable[Cause] | None = None) -> None:
        value = self.value(field) if field else None
        self.exclusions.append(Issue(rule, message, field, value, self._causes(rule, causes)))

    def _causes(self, rule: Rule, causes: Iterable[Cause] | None) -> tuple[Cause, ...]:
        if causes is None:
            return (Cause(rule, self.ref),)
        return tuple(dict.fromkeys(causes))

    def own_causes(self) -> tuple[Cause, ...]:
        """This row's failures, one hop: the rules it broke at its own line."""
        return tuple(Cause(issue.rule, self.ref) for issue in self.issues or self.exclusions)

    def root_causes(self) -> tuple[Cause, ...]:
        return tuple(
            dict.fromkeys(c for issue in self.issues or self.exclusions for c in issue.causes)
        )

    def freeze(self) -> RowResult:
        if self.issues:
            disposition, issues = Disposition.REJECTED, tuple(self.issues)
        elif self.exclusions:
            disposition, issues = Disposition.EXCLUDED, tuple(self.exclusions)
        else:
            disposition, issues = Disposition.ELIGIBLE, ()
        return RowResult(
            ref=self.ref,
            key=self.key,
            raw=self.row.raw,
            unit_key=self.unit_key,
            disposition=disposition,
            dependent=self.dependent,
            issues=issues,
            warnings=tuple(self.warnings),
            target=self.target,
        )


def _key(row: SourceRow, *fields: str) -> str:
    if not row.well_formed:
        return ""
    return "/".join(row.value(field) for field in fields)


def _blank(value: str) -> bool:
    return not value.strip()


def _lines(drafts: Iterable[_Draft]) -> str:
    return ", ".join(str(d.ref.line) for d in drafts)


def _index(drafts: list[_Draft], field: str) -> dict[str, list[_Draft]]:
    """Readable rows by exact key text. Keys are never trimmed, padded, or reinterpreted."""
    index: dict[str, list[_Draft]] = defaultdict(list)
    for draft in drafts:
        if draft.row.well_formed and not _blank(draft.value(field)):
            index[draft.value(field)].append(draft)
    return dict(index)


# --- Stage 1: source validation ------------------------------------------------------------


def _check_structure(draft: _Draft) -> None:
    row = draft.row
    if row.fields is None:
        draft.reject(Rule.SV_01, "Line is not valid CSV (unbalanced quotes or a line break).")
    elif len(row.fields) != len(row.header):
        draft.reject(
            Rule.SV_01, f"Expected {len(row.header)} fields, found {len(row.fields)}."
        )


def _check_duplicates(drafts: list[_Draft], fields: tuple[str, ...], rule: Rule) -> None:
    groups: dict[tuple[str, ...], list[_Draft]] = defaultdict(list)
    for draft in drafts:
        if not draft.row.well_formed:
            continue
        key = tuple(draft.value(field) for field in fields)
        if not any(_blank(part) for part in key):
            groups[key].append(draft)
    label = "/".join(fields)
    for key, group in groups.items():
        if len(group) < 2:
            continue
        for draft in group:
            others = _lines(d for d in group if d is not draft)
            draft.reject(
                rule,
                f"{label} {'/'.join(key)} also appears on line {others}; no row is chosen.",
                field=fields[-1] if len(fields) == 1 else None,
            )


def _require(draft: _Draft, fields: Iterable[str]) -> None:
    for field in fields:
        if _blank(draft.value(field)):
            draft.reject(Rule.SV_02, f"{field} is required.", field)


def _check_pattern(draft: _Draft, field: str, pattern: re.Pattern[str], rule: Rule,
                   description: str) -> None:
    value = draft.value(field)
    if not _blank(value) and not pattern.fullmatch(value):
        draft.reject(rule, f"{field} {value!r} must be {description}.", field)


def _check_code(draft: _Draft, field: str, codes: Iterable[str]) -> None:
    value = draft.value(field)
    if not _blank(value) and value not in codes:
        draft.reject(Rule.SV_07, f"{field} {value!r} is not a known code.", field)


def _validate_borrower(draft: _Draft) -> None:
    if not draft.open:
        return
    _require(draft, contract.REQUIRED_FIELDS[contract.BORROWERS_FILE])
    _check_pattern(draft, "CUST_NO", contract.CUST_NO_PATTERN, Rule.SV_03, "exactly 8 digits")
    _check_code(draft, "CUST_TYPE", contract.CUSTOMER_TYPES)
    _check_code(draft, "RECORD_STATUS", contract.RECORD_STATUSES)

    customer_type = draft.value("CUST_TYPE")
    if customer_type not in contract.CUSTOMER_TYPES:
        return
    person_fields = ("LAST_NAME", "FIRST_NAME", "MIDDLE_INIT")
    if customer_type == contract.INDIVIDUAL:
        _require(draft, ("LAST_NAME", "FIRST_NAME"))
        _check_lengths(draft, ("LAST_NAME", "FIRST_NAME"))
        if not _blank(draft.value("BUSINESS_NAME")):
            draft.reject(Rule.SV_08, "BUSINESS_NAME must be empty for an individual.",
                         "BUSINESS_NAME")
        initial = draft.value("MIDDLE_INIT").strip()
        if initial and not contract.MIDDLE_INITIAL_PATTERN.fullmatch(initial):
            draft.reject(Rule.SV_08, "MIDDLE_INIT must be a single letter.", "MIDDLE_INIT")
    else:
        _require(draft, ("BUSINESS_NAME",))
        _check_lengths(draft, ("BUSINESS_NAME",))
        for field in person_fields:
            if not _blank(draft.value(field)):
                draft.reject(
                    Rule.SV_08, f"{field} must be empty for customer type {customer_type}.", field
                )


def _check_lengths(draft: _Draft, fields: Iterable[str]) -> None:
    for field in fields:
        length = len(draft.value(field).strip())
        limit = contract.MAX_NAME_LENGTHS[field]
        if length > limit:
            draft.reject(
                Rule.SV_11,
                f"{field} is {length} characters; the limit is {limit}. It is not truncated.",
                field,
            )


def _validate_application(draft: _Draft) -> None:
    if not draft.open:
        return
    _require(draft, contract.REQUIRED_FIELDS[contract.APPLICATIONS_FILE])
    _check_pattern(draft, "APPL_NO", contract.APPL_NO_PATTERN, Rule.SV_03, "exactly 10 digits")
    _check_code(draft, "PROD_CD", contract.PRODUCTS)
    _check_code(draft, "APPL_STAT", contract.STATUSES)
    _check_pattern(draft, "REQ_AMT", contract.AMOUNT_PATTERN, Rule.SV_04,
                   "digits with exactly two decimal places and no symbols")
    _check_pattern(draft, "INT_RATE", contract.RATE_PATTERN, Rule.SV_05,
                   "six digits in thousandths of a percent (006875 = 6.875%)")
    _check_pattern(draft, "TERM_MOS", contract.TERM_PATTERN, Rule.SV_06, "1 to 3 digits")


def _validate_party(draft: _Draft) -> None:
    if not draft.open:
        return
    _require(draft, contract.REQUIRED_FIELDS[contract.PARTIES_FILE])
    _check_pattern(draft, "APPL_NO", contract.APPL_NO_PATTERN, Rule.SV_03, "exactly 10 digits")
    _check_pattern(draft, "CUST_NO", contract.CUST_NO_PATTERN, Rule.SV_03, "exactly 8 digits")
    _check_code(draft, "REL_CD", contract.ROLES)


# --- Exclusions (spec section 10) ------------------------------------------------------------


def _exclude_borrower(draft: _Draft) -> None:
    if draft.open and draft.value("RECORD_STATUS") == contract.DELETED:
        draft.exclude(Rule.EX_01, "Customer is logically deleted in the legacy system.",
                      "RECORD_STATUS")


def _exclude_application(draft: _Draft) -> None:
    if not draft.open:
        return
    if draft.value("PROD_CD") in contract.EXCLUDED_PRODUCTS:
        draft.exclude(Rule.EX_02, "Product is outside the conversion scope.", "PROD_CD")
    if draft.value("APPL_STAT") == contract.VOIDED:
        draft.exclude(Rule.EX_03, "Application was voided in the legacy system.", "APPL_STAT")


def _exclude_party(draft: _Draft, applications: dict[str, list[_Draft]]) -> None:
    if not draft.open:
        return
    if draft.value("REL_CD") == contract.SIGNER:
        draft.exclude(Rule.EX_04, "Authorized signers are not liable parties.", "REL_CD")
        return
    parents = applications.get(draft.value("APPL_NO"), [])
    if parents and all(parent.excluded for parent in parents):
        draft.exclude(
            Rule.EX_05,
            f"Application {draft.value('APPL_NO')} is excluded.",
            "APPL_NO",
            causes=(cause for parent in parents for cause in parent.own_causes()),
        )
        draft.dependent = True


# --- Stage 2: mapping ------------------------------------------------------------------------


def _map_borrower(draft: _Draft) -> None:
    if not draft.open:
        return
    code = draft.value("CUST_TYPE")
    borrower_type = contract.CUSTOMER_TYPES[code]
    if borrower_type is None:
        draft.reject(
            Rule.MP_01,
            f"Customer type {code!r} has no target mapping in v1 and is never mapped to business.",
            "CUST_TYPE",
        )
    if code == contract.INDIVIDUAL:
        name = individual_name(
            draft.value("FIRST_NAME"), draft.value("MIDDLE_INIT"), draft.value("LAST_NAME")
        )
    else:
        name = business_name(draft.value("BUSINESS_NAME"))
    if len(name) > contract.MAX_LEGAL_NAME_LENGTH:
        draft.reject(
            Rule.MP_04,
            f"Legal name is {len(name)} characters; the limit is "
            f"{contract.MAX_LEGAL_NAME_LENGTH}.",
        )
    if draft.open and borrower_type is not None:
        draft.target = MappedBorrower(draft.value("CUST_NO"), borrower_type, name)


def _map_application(draft: _Draft) -> None:
    if not draft.open:
        return
    product = contract.PRODUCTS[draft.value("PROD_CD")]
    status = contract.STATUSES[draft.value("APPL_STAT")]
    if product is None:
        draft.reject(Rule.MP_01, "Product code has no target mapping.", "PROD_CD")
    if status is None:
        draft.reject(Rule.MP_01, "Status code has no target mapping.", "APPL_STAT")
    amount = parse_amount(draft.value("REQ_AMT"))
    if amount <= 0:
        draft.reject(Rule.MP_02, "Requested amount must be greater than zero.", "REQ_AMT")
    term = parse_term(draft.value("TERM_MOS"))
    if not contract.MIN_TERM_MONTHS <= term <= contract.MAX_TERM_MONTHS:
        draft.reject(
            Rule.MP_03,
            f"Term must be {contract.MIN_TERM_MONTHS} to {contract.MAX_TERM_MONTHS} months.",
            "TERM_MOS",
        )
    if draft.open and product is not None and status is not None:
        draft.target = MappedApplication(
            source_system_id=draft.value("APPL_NO"),
            loan_product=product,
            requested_amount=amount,
            interest_rate=parse_rate(draft.value("INT_RATE")),
            term_months=term,
            status=status,
        )


def _map_party(draft: _Draft) -> None:
    if not draft.open:
        return
    role = contract.ROLES[draft.value("REL_CD")]
    if role is None:
        draft.reject(Rule.MP_01, "Relationship code has no target mapping.", "REL_CD")
        return
    draft.target = MappedParty(draft.value("APPL_NO"), draft.value("CUST_NO"), role)


# --- Stage 2: references and conversion units (spec section 8) ------------------------------


def _resolve_references(
    parties: list[_Draft],
    applications: dict[str, list[_Draft]],
    borrowers: dict[str, list[_Draft]],
) -> None:
    for draft in parties:
        if draft.row.well_formed and draft.value("APPL_NO") in applications:
            draft.unit_key = draft.value("APPL_NO")
        if not draft.open:
            continue
        application_no = draft.value("APPL_NO")
        customer_no = draft.value("CUST_NO")
        if application_no not in applications:
            draft.reject(
                Rule.RF_01, f"Application {application_no} is not in applications.csv.", "APPL_NO"
            )
        customers = borrowers.get(customer_no)
        if customers is None:
            draft.reject(
                Rule.RF_02,
                f"Customer {customer_no} is not in borrowers.csv; no placeholder is created.",
                "CUST_NO",
            )
        elif any(customer.issues for customer in customers):
            draft.reject(
                Rule.RF_03,
                f"Customer {customer_no} was rejected (borrowers.csv line "
                f"{_lines(c for c in customers if c.issues)}).",
                "CUST_NO",
                causes=(c for customer in customers for c in customer.own_causes()),
            )
        elif all(customer.excluded for customer in customers):
            draft.reject(
                Rule.RF_04,
                f"Customer {customer_no} is excluded but named on an in-scope application.",
                "CUST_NO",
                causes=(c for customer in customers for c in customer.own_causes()),
            )


def _resolve_units(
    applications: dict[str, list[_Draft]], parties: list[_Draft]
) -> list[tuple[str, list[_Draft], list[_Draft]]]:
    by_unit: dict[str, list[_Draft]] = defaultdict(list)
    for draft in parties:
        if draft.unit_key is not None:
            by_unit[draft.unit_key].append(draft)

    units = []
    for key, app_drafts in applications.items():
        unit_parties = by_unit.get(key, [])
        if len(app_drafts) == 1 and app_drafts[0].open:
            _check_unit(app_drafts[0], unit_parties)
        if any(app.issues for app in app_drafts):
            causes = tuple(c for app in app_drafts for c in app.root_causes())
            for party in unit_parties:
                if party.open:
                    party.reject(
                        Rule.RF_05,
                        f"Application {key} was rejected, so its relationships are not loaded.",
                        causes=causes,
                    )
                    party.dependent = True
        units.append((key, app_drafts, unit_parties))
    return units


def _check_unit(application: _Draft, parties: list[_Draft]) -> None:
    required = [p for p in parties if not p.excluded]
    rejected = [p for p in required if p.issues]
    primaries = [p for p in required if p.value("REL_CD") == contract.PRIMARY]
    loadable_primaries = [p for p in primaries if p.open]
    # Duplicate rows for one customer are SV-10, not conflicting primaries.
    primary_customers = {
        p.value("CUST_NO") for p in primaries if not _blank(p.value("CUST_NO"))
    }

    if rejected:
        application.reject(
            Rule.RF_07,
            f"Required relationships were rejected (application_parties.csv line "
            f"{_lines(rejected)}).",
            causes=(c for party in rejected for c in party.own_causes()),
        )
    if len(primary_customers) > 1:
        application.reject(
            Rule.RF_08,
            f"{len(primary_customers)} different primary customers "
            f"({', '.join(sorted(primary_customers))}; application_parties.csv line "
            f"{_lines(primaries)}); no row is chosen.",
        )
    if not loadable_primaries:
        explained = any(p in rejected for p in primaries)
        application.reject(
            Rule.RF_06,
            "No loadable primary borrower; none is promoted or created.",
            causes=() if explained else None,
        )


def _unit_disposition(applications: list[_Draft]) -> Disposition:
    if any(app.issues for app in applications):
        return Disposition.REJECTED
    if all(app.excluded for app in applications):
        return Disposition.EXCLUDED
    return Disposition.ELIGIBLE


def _warn_customers_without_applications(borrowers: list[_Draft], parties: list[_Draft]) -> None:
    named_on: dict[str, list[_Draft]] = defaultdict(list)
    for party in parties:
        if party.row.well_formed:
            named_on[party.value("CUST_NO")].append(party)
    for borrower in borrowers:
        if not borrower.open:
            continue
        relationships = named_on.get(borrower.key, [])
        if any(party.open for party in relationships):
            continue
        summary = ", ".join(
            f"{p.value('APPL_NO')} ({'excluded' if p.excluded else 'rejected'})"
            for p in relationships
        ) or "none"
        borrower.warnings.append(
            Issue(
                Rule.WN_01,
                f"Customer has no loaded relationship. Named on: {summary}.",
                causes=tuple(Cause(i.rule, p.ref) for p in relationships
                             for i in (p.issues or p.exclusions)),
            )
        )


# --- Totals and inventory --------------------------------------------------------------------


def _amount_totals(applications: list[_Draft]) -> AmountTotals:
    amounts: dict[Disposition, list[Decimal]] = defaultdict(list)
    unparseable = 0
    for draft in applications:
        text = draft.value("REQ_AMT") if draft.row.well_formed else ""
        if not contract.AMOUNT_PATTERN.fullmatch(text):
            unparseable += 1
            continue
        disposition = (
            Disposition.REJECTED if draft.issues
            else Disposition.EXCLUDED if draft.exclusions
            else Disposition.ELIGIBLE
        )
        amounts[disposition].append(parse_amount(text))
    return AmountTotals(
        eligible=exact_sum(amounts[Disposition.ELIGIBLE]),
        excluded=exact_sum(amounts[Disposition.EXCLUDED]),
        rejected=exact_sum(amounts[Disposition.REJECTED]),
        unparseable=unparseable,
    )


def _unmapped_inventory(drafts_by_file: dict[str, list[_Draft]]) -> tuple[UnmappedField, ...]:
    inventory = []
    for file, fields in contract.UNMAPPED_FIELDS.items():
        for field, pattern in fields.items():
            populated: Counter[Disposition] = Counter()
            nonconforming = 0
            for draft in drafts_by_file[file]:
                if not draft.row.well_formed or _blank(draft.value(field)):
                    continue
                populated[draft.freeze().disposition] += 1
                if not _conforms(draft.value(field), pattern):
                    nonconforming += 1
            inventory.append(
                UnmappedField(
                    file, field, MappingProxyType({d: populated[d] for d in Disposition}),
                    nonconforming,
                )
            )
    return tuple(inventory)


def _conforms(value: str, pattern: re.Pattern[str]) -> bool:
    if not pattern.fullmatch(value):
        return False
    if pattern is contract.DATE_PATTERN:
        try:
            datetime.strptime(value, "%Y%m%d")
        except ValueError:
            return False
    return True
