"""Readable report tables: identifiers, root causes, and raw source lines (backlog UI-006)."""

import csv
import html
import io
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from loan_lab.conversion.legacy import run_conversion
from loan_lab.main import create_app
from loan_lab.paths import find_project_root

PROJECT_ROOT = find_project_root(Path(__file__))
SAMPLE_DIR = PROJECT_ROOT / "sample_data" / "legacy"
KINDS = ("exceptions", "exclusions", "warnings")
LONG_CUST_NO = '<b>&"x"</b>=HYPERLINK("http://example.invalid")' + "0" * 2500
LONG_APPL_NO = "0000500113" + "0" * 40 + "<i>x</i>"
PREVIEW = 24


def csv_line(*fields: str) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow(fields)
    return out.getvalue()


LONG_PARTY_LINE = csv_line("0000500112", LONG_CUST_NO, "PRI")


def write_hostile_extract(target: Path) -> None:
    """The sample extract with a long, hostile customer number and application number."""
    shutil.copytree(SAMPLE_DIR, target)
    parties = target / "application_parties.csv"
    data = parties.read_bytes()
    assert data.count(b"0000500112,10015,PRI") == 1
    parties.write_bytes(data.replace(b"0000500112,10015,PRI", LONG_PARTY_LINE.encode()))
    applications = target / "applications.csv"
    data = applications.read_bytes()
    assert data.count(b"\r\n0000500113,") == 1
    applications.write_bytes(data.replace(b"\r\n0000500113,", f"\r\n{LONG_APPL_NO},".encode()))


def make_client(root: Path, source: Path) -> TestClient:
    evidence = root / "output" / "conversion"
    run_conversion(source, "run-1", conversion_root=root / "data" / "conversion",
                   evidence_root=evidence)
    return TestClient(create_app(database_path=root / "no-los.db", evidence_root=evidence))


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    with make_client(tmp_path_factory.mktemp("readable"), SAMPLE_DIR) as client:
        yield client


@pytest.fixture(scope="module")
def hostile(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    root = tmp_path_factory.mktemp("readable-hostile")
    write_hostile_extract(root / "extract")
    with make_client(root, root / "extract") as client:
        yield client


def page(client: TestClient, kind: str) -> str:
    response = client.get(f"/conversions/run-1/reports/{kind}")
    assert response.status_code == 200
    return response.text


def download(client: TestClient, kind: str) -> list[dict[str, str]]:
    data = client.get(f"/conversions/run-1/reports/{kind}/download").content
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline="")))


def full_values(body: str, field: str) -> list[str]:
    """The complete value of every disclosed `field`, as the browser will read it."""
    pattern = (rf'<details class="raw-more" data-field="{field}"><summary>.*?</summary>'
               r'<div class="raw-full"><code data-raw-full data-copy-label="[^"]*">(.*?)</code>')
    return [html.unescape(value) for value in re.findall(pattern, body, re.DOTALL)]


def inline_values(body: str, field: str) -> list[str]:
    pattern = rf'<code class="ident" data-field="{field}">(.*?)</code>'
    return [html.unescape(value) for value in re.findall(pattern, body, re.DOTALL)]


def unspreadsheet(value: str) -> str:
    return value[1:] if value.startswith("'") and value[1:2] in ("=", "+", "-", "@") else value


# --- Identifiers ------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_short_identifiers_are_inline_and_exact(sample: TestClient, kind: str) -> None:
    body = page(sample, kind)
    rows = download(sample, kind)

    assert 'class="raw"' not in body
    assert inline_values(body, "source-key") == [r["SOURCE_KEY"] for r in rows]
    assert inline_values(body, "unit-key") == [r["UNIT_KEY"] for r in rows if r["UNIT_KEY"]]
    assert full_values(body, "source-key") == []


def test_leading_zeros_are_kept(sample: TestClient) -> None:
    body = page(sample, "exceptions")

    assert '<code class="ident" data-field="unit-key">0000500109</code>' in body
    assert '<code class="ident" data-field="source-key">0000500109/00010099</code>' in body


def test_long_identifiers_show_a_preview_and_keep_the_full_value(hostile: TestClient) -> None:
    body = page(hostile, "exceptions")
    keys = full_values(body, "source-key")
    units = full_values(body, "unit-key")

    assert f"0000500112/{LONG_CUST_NO}" in keys
    assert LONG_APPL_NO in keys and LONG_APPL_NO in units
    preview = html.escape(LONG_APPL_NO[:PREVIEW], quote=False)
    assert f'<code class="ident" aria-hidden="true">{preview}…</code>' in body
    assert (f'<span class="visually-hidden">Unit key, {len(LONG_APPL_NO)} characters</span>'
            in body)


def test_identifiers_are_never_altered(hostile: TestClient) -> None:
    body = page(hostile, "exceptions")
    rows = download(hostile, "exceptions")
    keys = inline_values(body, "source-key") + full_values(body, "source-key")

    assert sorted(keys) == sorted(unspreadsheet(r["SOURCE_KEY"]) for r in rows)


# --- Hostile values ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_markup_in_values_is_escaped(hostile: TestClient, kind: str) -> None:
    body = page(hostile, kind)

    assert "<b>" not in body and "<i>x</i>" not in body
    if kind == "exceptions":
        assert "&lt;b&gt;&amp;&#34;x&#34;&lt;/b&gt;" in body
        assert "&lt;i&gt;x&lt;/i&gt;" in body


def test_copy_labels_are_fixed_text(hostile: TestClient) -> None:
    labels = set(re.findall(r'data-copy-label="([^"]*)"', page(hostile, "exceptions")))

    assert labels == {"Source key", "Unit key", "Source value", "Source line"}


# --- Source lines -----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_every_source_line_is_complete_behind_a_one_line_preview(
    sample: TestClient, kind: str
) -> None:
    body = page(sample, kind)
    rows = download(sample, kind)

    assert full_values(body, "source-line") == [unspreadsheet(r["SOURCE_LINE"]) for r in rows]
    assert body.count('<details class="raw-more" data-field="source-line">') == len(rows)


def test_long_source_lines_are_never_truncated(hostile: TestClient) -> None:
    lines = full_values(page(hostile, "exceptions"), "source-line")

    assert len(LONG_PARTY_LINE) > 2000
    assert LONG_PARTY_LINE in lines


def test_source_line_preview_is_short(hostile: TestClient) -> None:
    body = page(hostile, "exceptions")
    previews = re.findall(
        r'data-field="source-line"><summary><code class="ident" aria-hidden="true">(.*?)</code>',
        body, re.DOTALL,
    )

    assert previews
    assert all(len(html.unescape(p).removesuffix("…")) <= 40 for p in previews)


# --- Root causes ------------------------------------------------------------------------------


def test_root_cause_references_wrap_only_between_references(sample: TestClient) -> None:
    body = page(sample, "warnings")

    assert ('<span class="ref">RF-05 application_parties.csv:14</span>; '
            '<span class="ref">EX-05 application_parties.csv:17</span>') in body


@pytest.mark.parametrize("kind", KINDS)
def test_root_cause_text_is_unchanged(sample: TestClient, kind: str) -> None:
    body = page(sample, kind)
    cells = re.findall(r'<td class="refs" data-field="root-cause">(.*?)</td>', body, re.DOTALL)
    shown = [html.unescape(re.sub(r"<[^>]+>", "", cell)) for cell in cells]

    assert shown == [r["ROOT_CAUSE"] or "—" for r in download(sample, kind)]
