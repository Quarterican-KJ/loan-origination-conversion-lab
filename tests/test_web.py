import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from loan_lab.db import create_db_engine
from loan_lab.main import create_app
from loan_lab.models import (
    ApplicationParty,
    ApplicationStatus,
    Borrower,
    BorrowerType,
    Collateral,
    CollateralPledge,
    CollateralType,
    Lien,
    LienStatus,
    LoanApplication,
    LoanProduct,
    PartyRole,
)
from loan_lab.synthetic import PRESETS, seed_database
from loan_lab.web.formatting import money
from loan_lab.web.queries import DirectoryFilters
from loan_lab.web.routes import _page_links, _query_string

HOSTILE_NAME = '<script>alert("borrower")</script> & "Partners" LLC'
HOSTILE_DESCRIPTION = "<img src=x onerror=alert(1)> press"
HOSTILE_CREDITOR = "<b>Bold</b> Example Lender"
WILDCARD_NAME = "100% Example_Partners"


@dataclass(frozen=True)
class WebDatabase:
    path: Path
    hostile_id: int
    orphan_id: int


@pytest.fixture(scope="module")
def web_db(tmp_path_factory: pytest.TempPathFactory) -> WebDatabase:
    path = tmp_path_factory.mktemp("web") / "los.db"
    engine = create_db_engine(f"sqlite:///{path.as_posix()}")
    seed_database(engine, PRESETS["small"])
    with Session(engine) as session:
        hostile = Borrower(legal_name=HOSTILE_NAME, borrower_type=BorrowerType.BUSINESS)
        wildcard = Borrower(legal_name=WILDCARD_NAME, borrower_type=BorrowerType.INDIVIDUAL)
        collateral = Collateral(
            collateral_type=CollateralType.EQUIPMENT,
            description=HOSTILE_DESCRIPTION,
            appraised_value=None,
            valuation_date=None,
            owner=hostile,
            liens=[
                Lien(
                    creditor_name=HOSTILE_CREDITOR,
                    priority=1,
                    outstanding_balance=Decimal("1500.00"),
                    status=LienStatus.ACTIVE,
                )
            ],
        )
        hostile_application = LoanApplication(
            loan_product=LoanProduct.COMMERCIAL_TERM,
            requested_amount=Decimal("123456.78"),
            interest_rate=Decimal("8.2500"),
            term_months=60,
            status=ApplicationStatus.SUBMITTED,
            parties=[
                ApplicationParty(borrower=hostile, role=PartyRole.PRIMARY_BORROWER),
                ApplicationParty(borrower=wildcard, role=PartyRole.GUARANTOR),
            ],
            collateral_pledges=[CollateralPledge(collateral=collateral)],
        )
        orphan = LoanApplication(
            loan_product=LoanProduct.CONSUMER_PERSONAL,
            requested_amount=Decimal("5000.00"),
            interest_rate=Decimal("12.0000"),
            term_months=24,
            status=ApplicationStatus.DRAFT,
        )
        session.add_all([hostile_application, orphan])
        session.commit()
        result = WebDatabase(path, hostile_application.id, orphan.id)
    engine.dispose()
    return result


@pytest.fixture(scope="module")
def client(web_db: WebDatabase) -> Iterator[TestClient]:
    app = create_app(database_path=web_db.path)
    with TestClient(app) as test_client:
        yield test_client
    app.state.engine.dispose()


@pytest.fixture
def db(web_db: WebDatabase) -> Iterator[Session]:
    engine = create_db_engine(f"sqlite:///{web_db.path.as_posix()}")
    with Session(engine) as session:
        yield session
    engine.dispose()


def row_ids(html: str) -> list[int]:
    return [int(value) for value in re.findall(r'data-application-id="(\d+)"', html)]


def field(html: str, name: str) -> str:
    match = re.search(rf'data-field="{name}">(.*?)</dd>', html, re.DOTALL)
    assert match, name
    return match.group(1).strip()


def kpi(html: str, name: str) -> str:
    match = re.search(rf'data-kpi="{name}"[^>]*>([^<]*)<', html)
    assert match, name
    return match.group(1)


@contextmanager
def count_statements(engine: Engine) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


# --- Dashboard ----------------------------------------------------------------------------


def test_dashboard_shows_database_totals(client: TestClient, db: Session) -> None:
    count = db.scalar(select(func.count()).select_from(LoanApplication))
    volume = db.scalar(select(func.sum(LoanApplication.requested_amount)))
    unvalued = db.scalar(
        select(func.count()).select_from(Collateral).where(Collateral.appraised_value.is_(None))
    )

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert kpi(response.text, "applications") == f"{count:,}"
    assert money(volume) in response.text
    assert kpi(response.text, "unvalued") == f"{unvalued:,}"


def test_dashboard_product_and_status_breakdown(client: TestClient, db: Session) -> None:
    html = client.get("/").text

    for product in LoanProduct:
        expected = db.scalar(
            select(func.count()).select_from(LoanApplication).where(
                LoanApplication.loan_product == product
            )
        )
        row = re.search(rf'<tr data-product="{product.value}">(.*?)</tr>', html, re.DOTALL)
        assert row, product
        assert f'data-field="count">{expected:,}<' in row.group(1)
    for status in ApplicationStatus:
        expected = db.scalar(
            select(func.count()).select_from(LoanApplication).where(LoanApplication.status == status)
        )
        row = re.search(rf'<tr data-status="{status.value}">(.*?)</tr>', html, re.DOTALL)
        assert row, status
        assert f'data-field="count">{expected:,}<' in row.group(1)


# --- Directory: listing and pagination ----------------------------------------------------


def test_directory_first_page(client: TestClient, web_db: WebDatabase) -> None:
    response = client.get("/applications")

    assert response.status_code == 200
    assert row_ids(response.text) == list(range(1, 26))
    assert "Showing 1–25 of 27 applications" in response.text
    assert "Oak Ridge Properties LLC" in response.text
    assert "$500,000.00" in response.text
    assert "6.8750%" in response.text


def test_directory_second_page(client: TestClient, web_db: WebDatabase) -> None:
    html = client.get("/applications?page=2").text

    assert row_ids(html) == [web_db.hostile_id, web_db.orphan_id]
    assert "No primary borrower" in html


@pytest.mark.parametrize(("page", "expected"), [(1, range(1, 11)), (3, range(21, 28))])
def test_directory_page_size(client: TestClient, page: int, expected: range) -> None:
    html = client.get(f"/applications?page_size=10&page={page}").text

    assert row_ids(html) == list(expected)


def test_pagination_links_keep_page_size(client: TestClient) -> None:
    html = client.get("/applications?page_size=10&page=2").text

    assert 'href="?page_size=10&amp;page=3"' in html
    assert 'href="?page_size=10"' in html


def test_query_string_preserves_filters() -> None:
    filters = DirectoryFilters(
        q="oak & co", product=LoanProduct.HOME_EQUITY, status=ApplicationStatus.APPROVED,
        page=2, page_size=50,
    )

    assert _query_string(filters, 3) == (
        "q=oak+%26+co&product=home_equity&status=approved&page_size=50&page=3"
    )
    assert _query_string(DirectoryFilters(), 1) == ""


def test_page_links_collapse_gaps() -> None:
    assert _page_links(DirectoryFilters(page=10, page_size=10), 20) == [
        1, None, 8, 9, 10, 11, 12, None, 20,
    ]
    assert _page_links(DirectoryFilters(page=1), 1) == [1]


def test_page_beyond_last_returns_404(client: TestClient) -> None:
    response = client.get("/applications?page=99")

    assert response.status_code == 404
    assert row_ids(response.text) == []
    assert "beyond the last page" in response.text


# --- Directory: filters and search --------------------------------------------------------


def _expected_ids(db: Session, *conditions) -> list[int]:  # noqa: ANN002
    return list(db.scalars(select(LoanApplication.id).where(*conditions).order_by(LoanApplication.id)))


@pytest.mark.parametrize("product", list(LoanProduct))
def test_product_filter(client: TestClient, db: Session, product: LoanProduct) -> None:
    html = client.get(f"/applications?product={product.value}&page_size=100").text

    assert row_ids(html) == _expected_ids(db, LoanApplication.loan_product == product)


@pytest.mark.parametrize("status", list(ApplicationStatus))
def test_status_filter(client: TestClient, db: Session, status: ApplicationStatus) -> None:
    html = client.get(f"/applications?status={status.value}&page_size=100").text

    assert row_ids(html) == _expected_ids(db, LoanApplication.status == status)


def test_combined_filters(client: TestClient, db: Session) -> None:
    html = client.get("/applications?product=commercial_term&status=submitted&page_size=100").text

    expected = _expected_ids(
        db,
        LoanApplication.loan_product == LoanProduct.COMMERCIAL_TERM,
        LoanApplication.status == ApplicationStatus.SUBMITTED,
    )
    assert expected
    assert row_ids(html) == expected


def test_empty_filter_values_mean_all(client: TestClient) -> None:
    html = client.get("/applications?q=&product=&status=&page=&page_size=100").text

    assert len(row_ids(html)) == 27


@pytest.mark.parametrize("query", ["Oak Ridge", "oak ridge", "OAK RIDGE PROPERTIES"])
def test_search_by_borrower_name_is_case_insensitive(client: TestClient, query: str) -> None:
    assert row_ids(client.get("/applications", params={"q": query}).text) == [1, 2]


def test_search_matches_any_party(client: TestClient) -> None:
    assert row_ids(client.get("/applications", params={"q": "whitfield"}).text) == [1, 2]


@pytest.mark.parametrize("query", ["7", "#7", " 7 "])
def test_search_by_application_id(client: TestClient, query: str) -> None:
    assert 7 in row_ids(client.get("/applications", params={"q": query}).text)


@pytest.mark.parametrize("query", ["%", "_", "0% Ex"])
def test_search_escapes_like_wildcards(
    client: TestClient, web_db: WebDatabase, query: str
) -> None:
    assert row_ids(client.get("/applications", params={"q": query}).text) == [web_db.hostile_id]


def test_search_with_sql_syntax_is_treated_as_text(client: TestClient) -> None:
    response = client.get("/applications", params={"q": "' OR 1=1 --"})

    assert response.status_code == 200
    assert row_ids(response.text) == []


def test_huge_numeric_search_does_not_overflow(client: TestClient) -> None:
    response = client.get("/applications", params={"q": "9" * 30})

    assert response.status_code == 200
    assert row_ids(response.text) == []


@pytest.mark.parametrize(
    "query",
    [
        "page=0",
        "page=-1",
        "page=abc",
        "page=1.5",
        "page=99999999",
        "page_size=7",
        "page_size=abc",
        "page_size=1000",
        "product=bogus",
        "status=APPROVED",
        "status=approved%27--",
        f"q={'x' * 101}",
        "q=%00",
    ],
)
def test_invalid_directory_inputs_are_rejected(client: TestClient, query: str) -> None:
    response = client.get(f"/applications?{query}")

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "Invalid" in response.text
    assert "Traceback" not in response.text


# --- Detail -------------------------------------------------------------------------------


def test_detail_shows_terms_parties_collateral_and_liens(client: TestClient) -> None:
    response = client.get("/applications/1")

    assert response.status_code == 200
    html = response.text
    assert field(html, "requested-amount") == "$500,000.00"
    assert field(html, "interest-rate") == "6.8750%"
    roles = re.findall(r'data-party-role="(\w+)">\s*<td>([^<]+)</td>', html)
    assert roles == [
        ("primary_borrower", "Oak Ridge Properties LLC"),
        ("guarantor", "Dana R. Whitfield"),
    ]
    assert field(html, "appraised-value") == "$725,000.00"
    assert field(html, "valuation-date") == "2026-05-14"
    assert field(html, "pledged-amount") == "$500,000.00"
    assert "Oak Ridge Properties LLC" in field(html, "owner")
    assert "(Primary borrower)" in field(html, "owner")
    assert 'href="http://testserver/applications/2">#2</a>' in field(html, "shared-with")
    assert "Harbor Example Savings Bank" in html
    assert "$200,000.00" in html
    assert "not a computed legal ranking" in html


def test_detail_shows_missing_valuation_as_unknown(
    client: TestClient, web_db: WebDatabase
) -> None:
    html = client.get(f"/applications/{web_db.hostile_id}").text

    assert "Unknown" in field(html, "appraised-value")
    assert "Unknown" in field(html, "valuation-date")
    assert "Not specified" in field(html, "pledged-amount")
    assert "$0" not in html
    assert "unknown, not zero" in html


def test_detail_without_parties_or_collateral(client: TestClient, web_db: WebDatabase) -> None:
    response = client.get(f"/applications/{web_db.orphan_id}")

    assert response.status_code == 200
    assert "No parties recorded." in response.text
    assert "No collateral pledged to this application." in response.text


def test_missing_application_returns_404(client: TestClient) -> None:
    response = client.get("/applications/99999")

    assert response.status_code == 404
    assert "There is no loan application with ID 99999." in response.text


@pytest.mark.parametrize("value", ["abc", "0", "-1", "1.5", str(2**63)])
def test_invalid_application_id_is_rejected(client: TestClient, value: str) -> None:
    response = client.get(f"/applications/{value}")

    assert response.status_code == 400
    assert "Invalid request" in response.text


def test_unknown_route_returns_html_404(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Page not found" in response.text


# --- HTML escaping ------------------------------------------------------------------------


def test_directory_escapes_stored_names(client: TestClient) -> None:
    html = client.get("/applications?page=2").text

    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(&#34;borrower&#34;)&lt;/script&gt; &amp; &#34;Partners&#34; LLC" in html


def test_detail_escapes_stored_text(client: TestClient, web_db: WebDatabase) -> None:
    html = client.get(f"/applications/{web_db.hostile_id}").text

    assert "<script>alert" not in html
    assert "<img src=x" not in html
    assert "<b>Bold</b>" not in html
    assert "&lt;img src=x onerror=alert(1)&gt; press" in html
    assert "&lt;b&gt;Bold&lt;/b&gt; Example Lender" in html
    assert "100% Example_Partners" in html


def test_search_text_is_escaped_when_echoed(client: TestClient) -> None:
    payload = '"><script>alert(1)</script>'
    html = client.get("/applications", params={"q": payload}).text

    assert "<script>alert(1)" not in html
    assert 'value="&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in html


# --- Read-only behavior -------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("url", ["/", "/applications", "/applications/1"])
def test_write_methods_are_not_allowed(client: TestClient, method: str, url: str) -> None:
    response = client.request(method, url)

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert "read-only" in response.text


def test_browsing_does_not_modify_the_database(
    client: TestClient, web_db: WebDatabase
) -> None:
    before = hashlib.sha256(web_db.path.read_bytes()).hexdigest()

    for url in [
        "/",
        "/applications",
        "/applications?page=2&page_size=10&q=a&status=approved",
        "/applications/1",
        f"/applications/{web_db.hostile_id}",
        "/applications/99999",
        "/applications?page=abc",
    ]:
        client.get(url)
    client.post("/applications")

    assert hashlib.sha256(web_db.path.read_bytes()).hexdigest() == before


def test_web_engine_rejects_writes(client: TestClient) -> None:
    engine = client.app.state.engine  # type: ignore[attr-defined]

    with pytest.raises(OperationalError, match="readonly|read-only"):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM lien"))


def test_missing_database_is_reported_and_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    app = create_app(database_path=missing)

    with TestClient(app) as test_client:
        response = test_client.get("/")
        assert test_client.get("/health").json() == {"status": "ok"}

    assert response.status_code == 503
    assert "python -m loan_lab.synthetic --preset demo" in response.text
    assert not missing.exists()


def test_database_without_tables_is_reported(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    empty.touch()
    app = create_app(database_path=empty)

    with TestClient(app) as test_client:
        response = test_client.get("/applications")
    app.state.engine.dispose()

    assert response.status_code == 503
    assert "Database unavailable" in response.text
    assert empty.stat().st_size == 0


# --- Query efficiency ---------------------------------------------------------------------


def test_dashboard_uses_fixed_number_of_queries(client: TestClient) -> None:
    with count_statements(client.app.state.engine) as statements:  # type: ignore[attr-defined]
        client.get("/")

    assert len(statements) == 3


@pytest.mark.parametrize("page_size", [10, 100])
def test_directory_uses_two_queries_regardless_of_page_size(
    client: TestClient, page_size: int
) -> None:
    with count_statements(client.app.state.engine) as statements:  # type: ignore[attr-defined]
        client.get(f"/applications?page_size={page_size}&q=a")

    assert len(statements) == 2


def test_detail_query_count_does_not_grow_with_related_rows(
    client: TestClient, web_db: WebDatabase
) -> None:
    counts = []
    for application_id in (1, 2, web_db.hostile_id):
        with count_statements(client.app.state.engine) as statements:  # type: ignore[attr-defined]
            assert client.get(f"/applications/{application_id}").status_code == 200
        counts.append(len(statements))

    assert counts == [5, 5, 5]


# --- Assets and layout --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/css/app.css", "text/css"),
        ("/static/js/app.js", "javascript"),
        ("/static/js/theme.js", "javascript"),
    ],
)
def test_static_assets_are_served(client: TestClient, path: str, content_type: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert content_type in response.headers["content-type"]


def test_layout_defaults_to_dark_theme_with_persistent_toggle(client: TestClient) -> None:
    html = client.get("/").text

    assert '<html lang="en" data-theme="dark">' in html
    assert "data-theme-toggle" in html
    assert "Loan Origination &amp; Conversion Lab" in html
    assert "loan-lab-theme" in client.get("/static/js/theme.js").text
    assert "localStorage.setItem(THEME_KEY" in client.get("/static/js/app.js").text


def test_pages_load_no_external_resources(client: TestClient) -> None:
    html = client.get("/applications/1").text
    urls = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)

    assert urls
    assert all(url.startswith("http://testserver/") for url in urls)


def test_health_endpoint_still_returns_json(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
