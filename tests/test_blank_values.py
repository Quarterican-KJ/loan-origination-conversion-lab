"""Blank recorded values are shown as "Blank value"; nothing else about them changes."""

import csv
import dataclasses
import hashlib
import io
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from loan_lab.conversion.legacy import reports as report_module
from loan_lab.conversion.legacy import run_conversion
from loan_lab.conversion.legacy.reports import spreadsheet_safe
from loan_lab.main import create_app
from loan_lab.paths import find_project_root
from loan_lab.web.formatting import blank
from loan_lab.web.templating import templates

PROJECT_ROOT = find_project_root(Path(__file__))
SAMPLE_DIR = PROJECT_ROOT / "sample_data" / "legacy"
KINDS = ("exceptions", "exclusions", "warnings")
SAMPLE_RUN, CASES_RUN = "sample-1", "cases-1"
# The first report rows of CASES_RUN record these values, in this order.
DISPLAY_CASES = ("", "  \t ", "Blank value", "0", "false", '<b class="x">&amp;</b>')
BLANK = re.compile(r'<span class="blank-value" data-blank( data-field="[^"]+")? title="([^"]+)">'
                   r"Blank value</span>")


def run_with_display_cases(run_id: str, *, conversion_root: Path, evidence_root: Path) -> None:
    """A run of the sample extract whose reports record DISPLAY_CASES as their first values.

    The planner never records some of these (warnings name no field, and exclusions never have a
    blank value), so they are given to the report writer directly. Every report still verifies.
    """
    real = report_module._rows

    def rows(plan: Any, kind: Any) -> tuple[Any, ...]:
        found = list(real(plan, kind))
        for index, value in enumerate(DISPLAY_CASES[: len(found)]):
            found[index] = dataclasses.replace(
                found[index], field=found[index].field or "LAST_NAME", source_value=value
            )
        return tuple(found)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(report_module, "_rows", rows)
        run_conversion(SAMPLE_DIR, run_id, conversion_root=conversion_root,
                       evidence_root=evidence_root)


@pytest.fixture(scope="module")
def evidence(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("blank-values")
    conversion, evidence = root / "data" / "conversion", root / "output" / "conversion"
    run_conversion(SAMPLE_DIR, SAMPLE_RUN, conversion_root=conversion, evidence_root=evidence)
    run_with_display_cases(CASES_RUN, conversion_root=conversion, evidence_root=evidence)
    return evidence


@pytest.fixture(scope="module")
def client(evidence: Path) -> Iterator[TestClient]:
    with TestClient(create_app(database_path=evidence.parent / "no-los.db",
                               evidence_root=evidence)) as test_client:
        yield test_client


def page(client: TestClient, run_id: str, kind: str, query: str = "") -> str:
    response = client.get(f"/conversions/{run_id}/reports/{kind}{query}")
    assert response.status_code == 200
    return response.text


def field_cells(body: str) -> list[str]:
    return re.findall(r'<td class="nowrap" data-field="field-and-value">(.*?)</td>', body, re.DOTALL)


def archived(evidence: Path, run_id: str, kind: str) -> list[dict[str, str]]:
    data = (evidence / run_id / "reports" / f"{kind}.csv").read_bytes()
    return list(csv.DictReader(io.StringIO(data.decode("utf-8"), newline="")))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- The shared test and macro ---------------------------------------------------------------


@pytest.mark.parametrize("value", ["", " ", "   ", "\t", "\r\n", " \t\u00a0\u3000 "])
def test_empty_and_whitespace_only_text_is_blank(value: str) -> None:
    assert blank(value)


@pytest.mark.parametrize(
    "value", [None, "Blank value", "0", "0.00", "false", 0, 0.0, False, " x ", "\u200b", "&nbsp;"]
)
def test_missing_values_zero_false_and_text_are_not_blank(value: object) -> None:
    assert not blank(value)


def raw_value(value: str, **kwargs: Any) -> str:
    macro = templates.env.get_template("_conversion.html").module.raw_value
    return str(macro(value, "source-value", "Source value", **kwargs))


@pytest.mark.parametrize(
    ("value", "title"),
    [
        ("", "The recorded value is empty."),
        (" ", "The recorded value is only whitespace (1 characters)."),
        ("  \t ", "The recorded value is only whitespace (4 characters)."),
    ],
)
@pytest.mark.parametrize("disclose", [False, True])
def test_blank_report_values_show_the_indicator(value: str, title: str, disclose: bool) -> None:
    shown = raw_value(value, disclose=disclose)

    match = BLANK.fullmatch(shown)
    assert match, shown
    assert match.group(1) == ' data-field="source-value"'
    assert match.group(2) == title


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("Blank value", "Blank value"),
        ("0", "0"),
        ("false", "false"),
        (" 0 ", " 0 "),
        ('<b class="x">&amp;</b>', "&lt;b class=&#34;x&#34;&gt;&amp;amp;&lt;/b&gt;"),
    ],
)
def test_other_report_values_show_exactly_as_recorded(value: str, shown: str) -> None:
    assert raw_value(value) == f'<code class="ident" data-field="source-value">{shown}</code>'


def test_hostile_text_cannot_imitate_the_indicator() -> None:
    imitation = '<span class="blank-value" data-blank>Blank value</span>'

    for disclose in (False, True):
        shown = raw_value(imitation, disclose=disclose)
        assert BLANK.search(shown) is None and "<span class=\"blank-value\"" not in shown
        assert "&lt;span class=&#34;blank-value&#34; data-blank&gt;" in shown


def discrepancy_cells(expected: str | None, actual: str | None) -> tuple[str, str]:
    item = SimpleNamespace(rule="RC-04", check="field_value", file="borrowers.csv", line=12,
                           source_key="00010011", target_table="borrower", target_id=7,
                           field="last_name", expected=expected, actual=actual,
                           message="Synthetic discrepancy.")
    body = templates.env.get_template("_discrepancy_table.html").render(items=[item])
    cells = dict(re.findall(r'<td class="wrap-tight" data-field="(expected|actual)">(.*?)</td>',
                            body, re.DOTALL))
    return cells["expected"], cells["actual"]


def test_discrepancy_values_distinguish_blank_from_missing() -> None:
    expected, actual = discrepancy_cells("", None)
    assert BLANK.fullmatch(expected) and actual == '<span class="muted">—</span>'

    expected, actual = discrepancy_cells("   ", "Blank value")
    assert BLANK.fullmatch(expected) and actual == "Blank value"

    expected, actual = discrepancy_cells("0", "<i>x</i>")
    assert expected == "0" and actual == "&lt;i&gt;x&lt;/i&gt;"


# --- The three report pages -------------------------------------------------------------------


def test_the_sample_blank_last_name_shows_the_indicator(client: TestClient) -> None:
    body = page(client, SAMPLE_RUN, "exceptions")
    rows = re.findall(r"<tr data-report-row.*?</tr>", body, re.DOTALL)
    (row,) = [r for r in rows if "<code>borrowers.csv:12</code>" in r]
    (cell,) = field_cells(row)

    assert cell.startswith("<code>LAST_NAME</code>: ")
    assert BLANK.fullmatch(cell.removeprefix("<code>LAST_NAME</code>: "))


@pytest.mark.parametrize("kind", KINDS)
def test_every_report_page_shows_each_case_through_the_shared_macro(
    client: TestClient, evidence: Path, kind: str
) -> None:
    cells = field_cells(page(client, CASES_RUN, kind))
    recorded = archived(evidence, CASES_RUN, kind)
    cases = DISPLAY_CASES[: len(recorded)]

    assert len(cells) == len(recorded) and len(cases) >= 3
    for cell, row, value in zip(cells, recorded, cases):
        assert row["SOURCE_VALUE"] == value
        prefix = f"<code>{row['FIELD']}</code>: "
        assert cell.startswith(prefix), cell
        shown = cell.removeprefix(prefix)
        if value.strip():
            assert "data-blank" not in shown
            assert shown == raw_value(value), shown
        else:
            assert BLANK.fullmatch(shown), shown
    # Rows that name no field show the existing "nothing applies" dash, never the indicator.
    for cell, row in zip(cells, recorded):
        if not row["FIELD"]:
            assert cell == '<span class="muted">—</span>'


@pytest.mark.parametrize("kind", KINDS)
def test_only_blank_field_values_are_marked_and_markup_stays_escaped(
    client: TestClient, evidence: Path, kind: str
) -> None:
    body = page(client, CASES_RUN, kind)
    recorded = archived(evidence, CASES_RUN, kind)

    blanks = sum(1 for r in recorded if r["FIELD"] and not r["SOURCE_VALUE"].strip())
    assert blanks >= 2 and body.count("data-blank") == blanks
    assert '<b class="x">' not in body
    if len(recorded) >= len(DISPLAY_CASES):
        assert "&lt;b class=&#34;x&#34;&gt;&amp;amp;&lt;/b&gt;" in body


@pytest.mark.parametrize("kind", KINDS)
def test_search_matches_recorded_text_not_the_indicator(
    client: TestClient, evidence: Path, kind: str
) -> None:
    literal = page(client, CASES_RUN, kind, "?q=Blank+value")
    cells = field_cells(literal)

    assert 'data-matched="1"' in literal
    assert len(cells) == 1 and "data-blank" not in cells[0]
    assert cells[0].endswith('<code class="ident" data-field="source-value">Blank value</code>')
    # A whitespace-only query is no query: every row is listed, as before.
    total = len(archived(evidence, CASES_RUN, kind))
    everything = page(client, CASES_RUN, kind, "?q=+++")
    assert f'data-matched="{total}" data-total="{total}"' in everything


@pytest.mark.parametrize("kind", KINDS)
def test_downloads_and_archived_reports_keep_the_exact_values(
    client: TestClient, evidence: Path, kind: str
) -> None:
    path = evidence / CASES_RUN / "reports" / f"{kind}.csv"
    before = sha256(path)
    data = client.get(f"/conversions/{CASES_RUN}/reports/{kind}/download").content
    page(client, CASES_RUN, kind)

    downloaded = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline="")))
    recorded = archived(evidence, CASES_RUN, kind)
    assert downloaded == [{k: spreadsheet_safe(v) for k, v in r.items()} for r in recorded]
    assert [r["SOURCE_VALUE"] for r in recorded[: len(DISPLAY_CASES)]] == list(
        DISPLAY_CASES[: len(recorded)]
    )
    assert sum(r["SOURCE_VALUE"] == "Blank value" for r in downloaded) == 1
    assert sha256(path) == before
