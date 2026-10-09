"""Responsive layout of the Conversion Management pages.

The browser tests load each page in same-origin iframes at fixed widths inside a headless
Chrome or Edge, measure the rendered layout there, and read the measurements back from the
dumped DOM. They are skipped when neither browser is installed.
"""

import html
import json
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from loan_lab.conversion.legacy import run_conversion
from loan_lab.main import create_app
from loan_lab.paths import find_project_root
from loan_lab.scenarios import Scenario, run_scenario

PROJECT_ROOT = find_project_root(Path(__file__))
SAMPLE_DIR = PROJECT_ROOT / "sample_data" / "legacy"
WIDTHS = (390, 768, 1280, 1920)
DEFECT_RUN = "SYN-DEFECT-20261009T000000Z-abc123"
CTRL_RUN = "SYN-CTRL-20261009T000000Z-def456"
SHORT_RUN = "DEMO-001"
PAGES = (
    "/conversions",
    f"/conversions/{DEFECT_RUN}",
    f"/conversions/{CTRL_RUN}",
    f"/conversions/{DEFECT_RUN}/reconciliation",
)
BROWSERS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)

HARNESS = """<!doctype html>
<html><head><meta charset="utf-8"><title>Layout harness</title></head>
<body>
<pre id="layout-results">pending</pre>
<script>
const PAGES = %(pages)s;
const WIDTHS = %(widths)s;

function measure(win) {
  const doc = win.document;
  const root = doc.documentElement;
  const visible = (el) => !!el && !el.hidden && el.getClientRects().length > 0;
  const regions = [...doc.querySelectorAll(".table-scroll")].map((region) => {
    const table = region.querySelector("table");
    const hint = region.previousElementSibling;
    const hasHint = !!hint && hint.hasAttribute("data-scroll-hint");
    const cells = [...table.rows].map((row) => row.cells[row.cells.length - 1]);
    region.scrollLeft = region.scrollWidth;
    const box = region.getBoundingClientRect();
    const lastColumnReachable = cells.every((cell) => cell.getBoundingClientRect().right <= box.right + 1);
    const scrolledTo = region.scrollLeft;
    region.scrollLeft = 0;
    const firstColumnVisible = [...table.rows].every((row) => row.cells[0].getBoundingClientRect().left >= box.left - 1);
    region.focus();
    const focused = doc.activeElement === region;
    region.blur();
    return {
      label: region.getAttribute("aria-label"),
      role: region.getAttribute("role"),
      scrollWidth: region.scrollWidth,
      clientWidth: region.clientWidth,
      overflowing: region.scrollWidth > region.clientWidth + 1,
      flagged: region.hasAttribute("data-overflowing"),
      tabIndex: region.getAttribute("tabindex"),
      focused,
      overflowX: win.getComputedStyle(region).overflowX,
      hintVisible: hasHint && visible(hint),
      describedBy: region.getAttribute("aria-describedby"),
      hintId: hasHint ? hint.id : null,
      scrolledTo,
      lastColumnReachable,
      firstColumnVisible,
    };
  });
  const runLinks = [...doc.querySelectorAll("a code.run-id")].map((code) => {
    const link = code.closest("a");
    const hidden = code.querySelector(".visually-hidden");
    const shown = code.querySelector("[aria-hidden='true']");
    return {
      href: link.getAttribute("href"),
      title: code.getAttribute("title"),
      hiddenText: hidden ? hidden.textContent : null,
      shownText: shown ? shown.textContent : code.textContent,
      linkWidth: Math.round(link.getBoundingClientRect().width),
    };
  });
  const viewport = root.clientWidth;
  const outside = [...doc.querySelectorAll("main *")]
    .filter((el) => !el.closest(".table-scroll") && visible(el))
    .filter((el) => el.getBoundingClientRect().right > viewport + 1 || el.getBoundingClientRect().left < -1)
    .map((el) => el.tagName.toLowerCase() + (el.className ? "." + el.className : ""));
  return {
    innerWidth: win.innerWidth,
    pageScrollWidth: root.scrollWidth,
    pageClientWidth: root.clientWidth,
    bodyScrollWidth: doc.body.scrollWidth,
    looseTables: [...doc.querySelectorAll("table")].filter((t) => !t.closest(".table-scroll")).length,
    clippedPanels: [...doc.querySelectorAll(".panel")].filter((p) => p.scrollWidth > p.clientWidth + 1).length,
    outside: outside.slice(0, 10),
    headersWithoutScope: [...doc.querySelectorAll("th")].filter((th) => !th.hasAttribute("scope")).length,
    regions,
    runLinks,
  };
}

function load(page, width) {
  return new Promise((resolve) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = `width:${width}px;height:900px;border:0;display:block`;
    frame.addEventListener("load", () => setTimeout(() => {
      let result;
      try { result = measure(frame.contentWindow); } catch (error) { result = {error: String(error)}; }
      frame.remove();
      resolve(result);
    }, 400));
    frame.src = page;
    document.body.append(frame);
  });
}

(async () => {
  const results = {};
  for (const page of PAGES) {
    for (const width of WIDTHS) results[`${page} @ ${width}`] = await load(page, width);
  }
  document.getElementById("layout-results").textContent = JSON.stringify(results);
})();
</script>
</body></html>
"""


@pytest.fixture(scope="module")
def evidence_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("layout")
    roots = {
        "conversion_root": root / "data" / "conversion",
        "evidence_root": root / "output" / "conversion",
        "scenario_root": root / "data" / "scenarios",
    }
    run_conversion(SAMPLE_DIR, SHORT_RUN, conversion_root=roots["conversion_root"],
                   evidence_root=roots["evidence_root"])
    run_scenario(Scenario.LOADER_DEFECT, DEFECT_RUN, **roots)
    run_scenario(Scenario.CONTROL_TOTALS, CTRL_RUN, **roots)
    return roots["evidence_root"]


def make_app(evidence_root: Path):
    return create_app(database_path=evidence_root.parent / "no-los.db", evidence_root=evidence_root)


# --- Markup that the responsive layout relies on (no browser needed) ------------------------


@pytest.fixture(scope="module")
def client(evidence_root: Path) -> Iterator[TestClient]:
    with TestClient(make_app(evidence_root)) as test_client:
        yield test_client


@pytest.mark.parametrize("page", PAGES)
def test_every_table_sits_in_a_labeled_scroll_region(client: TestClient, page: str) -> None:
    body = client.get(page).text
    tables = body.count("<table")
    regions = re.findall(r'<div class="table-scroll" role="region" aria-label="[^"]+">\s*<table',
                         body)
    assert tables and len(regions) == tables


def test_long_run_ids_are_compact_but_keep_their_full_value(client: TestClient) -> None:
    body = client.get("/conversions").text
    link = re.search(rf'<a href="([^"]+)"><code class="run-id run-id-compact" '
                     rf'title="{DEFECT_RUN}"><span aria-hidden="true">([^<]+)</span>'
                     rf'<span class="visually-hidden">{DEFECT_RUN}</span></code></a>', body)
    assert link, "long run ID is not rendered compactly with its full value"
    assert link.group(1).endswith(f"/conversions/{DEFECT_RUN}")
    assert link.group(2) == f"{DEFECT_RUN[:14]}…{DEFECT_RUN[-6:]}"
    assert client.get(link.group(1)).status_code == 200


def test_short_run_ids_are_shown_in_full(client: TestClient) -> None:
    body = client.get("/conversions").text
    assert f'<code class="run-id">{SHORT_RUN}</code>' in body
    assert f'title="{SHORT_RUN}"' not in body


def test_detail_heading_keeps_the_full_run_id(client: TestClient) -> None:
    body = client.get(f"/conversions/{DEFECT_RUN}").text
    heading = re.search(r"<h1>(.*?)</h1>", body, re.DOTALL).group(1)
    assert DEFECT_RUN in heading and "…" not in heading


# --- Rendered layout in a real browser ------------------------------------------------------


def find_browser() -> Path | None:
    for candidate in BROWSERS:
        if candidate.is_file():
            return candidate
    for name in ("chrome", "msedge", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def layout(evidence_root: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    browser = find_browser()
    if browser is None:
        pytest.skip("no Chrome or Edge installation found for layout tests")
    app = make_app(evidence_root)
    harness = HARNESS % {"pages": json.dumps(PAGES), "widths": json.dumps(WIDTHS)}
    app.add_api_route("/__layout-harness", lambda: HTMLResponse(harness), include_in_schema=False)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                pytest.fail("layout test server did not start")
            time.sleep(0.05)
        profile = tmp_path_factory.mktemp("browser-profile")
        completed = subprocess.run(
            [str(browser), "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions", "--hide-scrollbars",
             f"--user-data-dir={profile}", "--window-size=2000,1200",
             "--virtual-time-budget=60000", "--dump-dom",
             f"http://127.0.0.1:{port}/__layout-harness"],
            capture_output=True, text=True, encoding="utf-8", timeout=180, check=False,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    match = re.search(r'<pre id="layout-results">(.*?)</pre>', completed.stdout, re.DOTALL)
    assert match and match.group(1) != "pending", (
        f"browser produced no measurements (exit {completed.returncode}): {completed.stderr[-2000:]}"
    )
    return json.loads(html.unescape(match.group(1)))


CASES = [(page, width) for page in PAGES for width in WIDTHS]


def case(layout: dict[str, Any], page: str, width: int) -> dict[str, Any]:
    result = layout[f"{page} @ {width}"]
    assert "error" not in result, result.get("error")
    assert result["innerWidth"] == width
    return result


@pytest.mark.parametrize(("page", "width"), CASES)
def test_page_never_scrolls_horizontally(layout: dict[str, Any], page: str, width: int) -> None:
    result = case(layout, page, width)
    assert result["pageScrollWidth"] <= result["pageClientWidth"], result
    assert result["bodyScrollWidth"] <= result["pageClientWidth"], result
    assert result["outside"] == []
    assert result["clippedPanels"] == 0


@pytest.mark.parametrize(("page", "width"), CASES)
def test_every_column_is_reachable(layout: dict[str, Any], page: str, width: int) -> None:
    result = case(layout, page, width)
    assert result["looseTables"] == 0
    assert result["regions"]
    for region in result["regions"]:
        assert region["overflowX"] == "auto", region
        assert region["lastColumnReachable"] and region["firstColumnVisible"], region
        if region["overflowing"]:
            assert region["scrolledTo"] > 0, region


@pytest.mark.parametrize(("page", "width"), CASES)
def test_overflow_is_announced_and_keyboard_scrollable_only_when_needed(
    layout: dict[str, Any], page: str, width: int
) -> None:
    for region in case(layout, page, width)["regions"]:
        assert region["role"] == "region" and region["label"], region
        assert region["flagged"] == region["overflowing"], region
        if region["overflowing"]:
            assert region["tabIndex"] == "0" and region["focused"], region
            assert region["hintVisible"], region
            assert region["describedBy"] == region["hintId"], region
        else:
            assert region["tabIndex"] is None and not region["focused"], region
            assert not region["hintVisible"], region
            assert region["describedBy"] is None, region


@pytest.mark.parametrize(("page", "width"), CASES)
def test_table_headers_have_scope(layout: dict[str, Any], page: str, width: int) -> None:
    assert case(layout, page, width)["headersWithoutScope"] == 0


@pytest.mark.parametrize("width", WIDTHS)
def test_run_links_show_compact_ids_with_full_values(layout: dict[str, Any], width: int) -> None:
    links = {link["href"].rsplit("/", 1)[-1]: link
             for link in case(layout, "/conversions", width)["runLinks"]}
    assert set(links) == {DEFECT_RUN, CTRL_RUN, SHORT_RUN}
    for run_id in (DEFECT_RUN, CTRL_RUN):
        link = links[run_id]
        assert link["title"] == link["hiddenText"] == run_id
        assert link["shownText"] == f"{run_id[:14]}…{run_id[-6:]}"
    assert links[SHORT_RUN]["shownText"] == SHORT_RUN and links[SHORT_RUN]["title"] is None


def test_wide_desktop_shows_the_run_list_without_scrolling(layout: dict[str, Any]) -> None:
    (region,) = case(layout, "/conversions", 1920)["regions"]
    assert not region["overflowing"], region


def test_mobile_run_list_scrolls_inside_its_region(layout: dict[str, Any]) -> None:
    (region,) = case(layout, "/conversions", 390)["regions"]
    assert region["overflowing"] and region["hintVisible"], region
