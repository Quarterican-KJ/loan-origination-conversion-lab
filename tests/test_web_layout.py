"""Responsive layout of the Conversion Management pages.

The browser tests load each page in same-origin iframes at fixed widths inside a headless
Chrome or Edge, measure the rendered layout there, and read the measurements back from the
dumped DOM. They are skipped when neither browser is installed.
"""

import dataclasses
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

from loan_lab.conversion.legacy import reconcile_run, run_conversion
from loan_lab.conversion.legacy import run as runs
from loan_lab.conversion.legacy.contract import Rule
from loan_lab.conversion.legacy.plan import Cause, Disposition, Issue
from loan_lab.db import create_db_engine
from loan_lab.main import create_app
from loan_lab.paths import find_project_root
from loan_lab.scenarios import Scenario, run_scenario
from loan_lab.synthetic import PRESETS, seed_database

from test_blank_values import run_with_display_cases
from test_report_readability import write_hostile_extract

PROJECT_ROOT = find_project_root(Path(__file__))
SAMPLE_DIR = PROJECT_ROOT / "sample_data" / "legacy"
WIDTHS = (390, 768, 1280, 1920)
DEFECT_RUN = "SYN-DEFECT-20261009T000000Z-abc123"
CTRL_RUN = "SYN-CTRL-20261009T000000Z-def456"
SHORT_RUN = "DEMO-001"
HOSTILE_RUN = "HOSTILE-001"
CASES_RUN = "CASES-001"
INCOMPLETE_RUN = "INCOMPLETE-001"
REPORT_PAGES = (
    f"/conversions/{SHORT_RUN}/reports/exceptions",
    f"/conversions/{SHORT_RUN}/reports/exclusions",
    f"/conversions/{SHORT_RUN}/reports/warnings",
    f"/conversions/{HOSTILE_RUN}/reports/exceptions",
)
LOS_PAGES = ("/", "/applications?page_size=100", "/applications/12")
PAGES = (
    *LOS_PAGES,
    "/conversions",
    f"/conversions/{DEFECT_RUN}",
    f"/conversions/{CTRL_RUN}",
    f"/conversions/{DEFECT_RUN}/reconciliation",
    *REPORT_PAGES,
)
# The sample's blank LAST_NAME, and every report of a run that records blank and look-alike values.
BLANK_PAGES = (
    f"/conversions/{SHORT_RUN}/reports/exceptions",
    *(f"/conversions/{CASES_RUN}/reports/{kind}" for kind in ("exceptions", "exclusions", "warnings")),
)
# Every page that draws status badges. The defect run's reconciliation shows PASS and FAIL, the
# incomplete run's shows INCOMPLETE.
BADGE_PAGES = (
    *LOS_PAGES,
    "/conversions",
    f"/conversions/{DEFECT_RUN}",
    f"/conversions/{DEFECT_RUN}/reconciliation",
    f"/conversions/{INCOMPLETE_RUN}",
    f"/conversions/{INCOMPLETE_RUN}/reconciliation",
)
RULE_RESULT_PAGES = (
    f"/conversions/{DEFECT_RUN}/reconciliation",
    f"/conversions/{INCOMPLETE_RUN}/reconciliation",
)
# Badges whose text is drawn in --success or --danger, and the INCOMPLETE result.
STATUS_BADGES = (
    "result-pass", "result-fail", "result-incomplete", "condition-reconciled", "condition-failed",
    "run-status-reconciled", "run-status-failed", "status-approved", "status-declined",
    "pledge-active", "lien-active",
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
const BLANK_PAGES = %(blank_pages)s;
const BADGE_PAGES = %(badge_pages)s;

// Every badge in both themes: its text colour, and the colour actually behind it (its tint
// composited over the opaque surface below), at rest and with its table row hovered.
function measureBadges(win) {
  const doc = win.document;
  const root = doc.documentElement;
  const parse = (color) => {
    const v = color.match(/[0-9.]+/g).map(Number);
    return [v[0], v[1], v[2], v.length > 3 ? v[3] : 1];
  };
  const behind = (el) => {
    const layers = [];
    for (; el; el = el.parentElement) {
      const c = parse(win.getComputedStyle(el).backgroundColor);
      if (c[3] > 0) layers.push(c);
      if (c[3] >= 1) break;
    }
    let base = [255, 255, 255];
    for (const c of layers.reverse()) base = base.map((b, i) => c[i] * c[3] + b * (1 - c[3]));
    return `rgb(${base.map(Math.round).join(", ")})`;
  };
  const themes = {};
  for (const theme of ["dark", "light"]) {
    root.dataset.theme = theme;
    themes[theme] = [...doc.querySelectorAll("main .badge")].map((el) => {
      const style = win.getComputedStyle(el);
      const row = el.closest(".table-hover tbody tr");
      let hovered = null;
      if (row) {
        row.style.background = "var(--surface-2)";
        hovered = behind(el);
        row.style.background = "";
      }
      return {
        classes: [...el.classList],
        text: el.textContent.trim(),
        result: el.dataset.result || null,
        color: style.color,
        background: behind(el),
        hovered,
        borderStyle: style.borderTopStyle,
        borderColor: style.borderTopColor,
      };
    });
  }
  root.dataset.theme = "dark";
  return themes;
}

// Each "Blank value" indicator, and each recorded value that literally reads "Blank value", as
// drawn in both themes.
function measureBlanks(win) {
  const doc = win.document;
  const root = doc.documentElement;
  const background = (el) => {
    for (; el; el = el.parentElement) {
      const color = win.getComputedStyle(el).backgroundColor;
      if (color !== "rgba(0, 0, 0, 0)" && color !== "transparent") return color;
    }
    return "rgb(255, 255, 255)";
  };
  const describe = (el) => {
    const style = win.getComputedStyle(el);
    return {
      text: el.textContent,
      color: style.color,
      background: background(el),
      border: style.borderTopStyle,
      fontStyle: style.fontStyle,
      fontFamily: style.fontFamily,
      lines: el.getClientRects().length,
      width: el.getBoundingClientRect().width,
    };
  };
  const themes = {};
  for (const theme of ["dark", "light"]) {
    root.dataset.theme = theme;
    themes[theme] = {
      blanks: [...doc.querySelectorAll("main table [data-blank]")].map(describe),
      literals: [...doc.querySelectorAll("main table code.ident")]
        .filter((code) => code.textContent === "Blank value").map(describe),
    };
  }
  root.dataset.theme = "dark";
  return themes;
}

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
  const unbroken = [...doc.querySelectorAll(".report-table code.ident, .report-table .ref")];
  const brokenValues = unbroken.filter((el) => el.getClientRects().length > 1)
    .map((el) => el.textContent.slice(0, 40));
  const summaries = [...doc.querySelectorAll(".raw-more summary")];
  const focusableSummaries = summaries.filter((summary) => {
    summary.focus();
    const focused = doc.activeElement === summary;
    summary.blur();
    return focused;
  }).length;
  const report = measureReport(win);
  const disclosures = [...doc.querySelectorAll("details.raw-more")];
  disclosures.forEach((details) => { details.open = true; });
  const fulls = [...doc.querySelectorAll("[data-raw-full]")];
  const opened = {
    pageScrollWidth: root.scrollWidth,
    pageClientWidth: root.clientWidth,
    values: fulls.length,
    visible: fulls.filter(visible).length,
    narrow: fulls.filter((el) => el.getBoundingClientRect().width < 200).length,
    clipped: fulls.filter((el) => el.scrollWidth > el.clientWidth + 1).length,
    copyButtons: fulls.filter((el) => el.nextElementSibling && el.nextElementSibling.matches("button.button-copy")).length,
    lastColumnReachable: [...doc.querySelectorAll(".table-scroll")].every((region) => {
      region.scrollLeft = region.scrollWidth;
      const right = region.getBoundingClientRect().right;
      const reachable = [...region.querySelector("table").rows]
        .every((row) => row.cells[row.cells.length - 1].getBoundingClientRect().right <= right + 1);
      region.scrollLeft = 0;
      return reachable;
    }),
  };
  disclosures.forEach((details) => { details.open = false; });
  return {
    report,
    brokenValues,
    summaries: summaries.length,
    focusableSummaries,
    opened,
    innerWidth: win.innerWidth,
    pageScrollWidth: root.scrollWidth,
    pageClientWidth: root.clientWidth,
    bodyScrollWidth: doc.body.scrollWidth,
    looseTables: [...doc.querySelectorAll("table")].filter((t) => !t.closest(".table-scroll")).length,
    clippedPanels: [...doc.querySelectorAll(".panel")].filter((p) => p.scrollWidth > p.clientWidth + 1).length,
    clippedBy: [...doc.querySelectorAll(".panel")].filter((p) => p.scrollWidth > p.clientWidth + 1)
      .flatMap((p) => [...p.querySelectorAll("*")]
        .filter((el) => !el.closest(".table-scroll") && el.getBoundingClientRect().right > p.getBoundingClientRect().right + 1)
        .map((el) => `${el.tagName.toLowerCase()}.${el.className} ${Math.round(el.getBoundingClientRect().right - p.getBoundingClientRect().right)}px`)),
    outside: outside.slice(0, 10),
    headersWithoutScope: [...doc.querySelectorAll("th")].filter((th) => !th.hasAttribute("scope")).length,
    regions,
    runLinks,
  };
}

// The report table's last column (source line) and sticky leading columns, at initial and full
// horizontal scroll, with every value collapsed and then open.
function measureReport(win) {
  const doc = win.document;
  const root = doc.documentElement;
  const table = doc.querySelector("table[data-report-table]");
  if (!table) return null;
  const region = table.closest(".table-scroll");
  const visible = (el) => !!el && el.getClientRects().length > 0;
  const rect = (el) => el.getBoundingClientRect();

  function coverRight(row) {
    return table.hasAttribute("data-sticky") ? rect(row.querySelector(".col-key")).right : rect(region).left;
  }

  function lastColumn(state) {
    region.scrollLeft = region.scrollWidth;
    const box = rect(region);
    const problems = new Set();
    const cells = [...table.querySelectorAll(".col-source-line")];
    for (const cell of cells) {
      const r = rect(cell);
      const padRight = parseFloat(win.getComputedStyle(cell).paddingRight);
      if (r.left < coverRight(cell.parentElement) - 1) problems.add("covered by sticky columns");
      if (r.right > box.right + 1) problems.add("cut off at the right edge");
      for (const child of cell.querySelectorAll("summary, summary > code, .raw-full, .raw-full code, .raw-full button")) {
        if (!visible(child)) continue;
        const c = rect(child);
        if (c.right > r.right - padRight + 1 || c.left < r.left - 1) problems.add(`content outside its cell: ${child.tagName}`);
      }
      for (const code of cell.querySelectorAll(".raw-full code")) {
        if (visible(code) && code.scrollWidth > code.clientWidth + 1) problems.add("full value clipped");
      }
      const summary = cell.querySelector("summary");
      if (summary) {
        const style = win.getComputedStyle(summary);
        const line = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.5;
        if (rect(summary).height > line * 1.8) problems.add("preview wraps");
      }
    }
    const header = table.querySelector("th.col-source-line");
    const result = {
      state,
      sticky: table.hasAttribute("data-sticky"),
      width: Math.round(rect(header).width),
      rem: parseFloat(win.getComputedStyle(root).fontSize),
      padRight: parseFloat(win.getComputedStyle(header).paddingRight),
      gapToEdge: Math.round(box.right - rect(header).right),
      pageOverflow: root.scrollWidth - root.clientWidth,
      problems: [...problems],
    };
    region.scrollLeft = 0;
    return result;
  }

  function stickyColumns() {
    if (!table.hasAttribute("data-sticky")) return null;
    region.scrollLeft = region.scrollWidth;
    const box = rect(region);
    const rows = [...table.rows];
    const result = {
      sourceAtEdge: rows.every((row) => Math.abs(rect(row.querySelector(".col-source")).left - box.left) <= 1),
      noOverlap: rows.every((row) => rect(row.querySelector(".col-key")).left >= rect(row.querySelector(".col-source")).right - 1),
      share: Math.round(100 * (rect(rows[0].querySelector(".col-key")).right - box.left) / region.clientWidth),
      backgrounds: {},
    };
    for (const theme of ["dark", "light"]) {
      root.dataset.theme = theme;
      const panel = win.getComputedStyle(table.closest(".panel")).backgroundColor;
      result.backgrounds[theme] = [...table.querySelectorAll("td.col-source, td.col-key")]
        .every((cell) => win.getComputedStyle(cell).backgroundColor === panel);
    }
    root.dataset.theme = "dark";
    region.scrollLeft = 0;
    return result;
  }

  function focusProbe(selector) {
    const target = table.querySelector(selector);
    if (!target) return null;
    region.scrollLeft = 0;
    target.focus();
    const box = rect(region);
    const r = rect(target);
    const result = {
      selector,
      focused: doc.activeElement === target,
      inView: r.left >= coverRight(target.closest("tr")) - 1 && r.right <= box.right + 1,
      left: Math.round(r.left),
      right: Math.round(r.right),
      coverRight: Math.round(coverRight(target.closest("tr"))),
      regionRight: Math.round(box.right),
      scrollLeft: region.scrollLeft,
      overflow: region.scrollWidth - region.clientWidth,
    };
    target.blur();
    region.scrollLeft = 0;
    return result;
  }

  const details = [...table.querySelectorAll("details.raw-more")];
  const initialSticky = table.hasAttribute("data-sticky");
  const collapsed = lastColumn("collapsed");
  const sticky = stickyColumns();
  const focus = [
    focusProbe("td.col-source-line summary"),
    focusProbe("td:not(.col-source):not(.col-key):not(.col-source-line) summary"),
  ].filter(Boolean);
  details.forEach((d) => { d.open = true; });
  const expanded = lastColumn("expanded");
  details.forEach((d) => { d.open = false; });
  return {initialSticky, collapsed, expanded, sticky, focus};
}

function load(page, width, measurement = measure) {
  return new Promise((resolve) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = `width:${width}px;height:900px;border:0;display:block`;
    frame.addEventListener("load", () => setTimeout(() => {
      let result;
      try {
        result = measurement(frame.contentWindow);
      } catch (error) { result = {error: String(error)}; }
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
  for (const page of BLANK_PAGES) results[`blanks ${page}`] = await load(page, 1280, measureBlanks);
  for (const page of BADGE_PAGES) results[`badges ${page}`] = await load(page, 1280, measureBadges);
  document.getElementById("layout-results").textContent = JSON.stringify(results);
})();
</script>
</body></html>
"""


def convert_with_incomplete_rc12(run_id: str, roots: dict[str, Path]) -> None:
    """Records borrowers.csv:12 (SV-02) as an EX-01 exclusion, so RC-11 fails and RC-12 is
    INCOMPLETE."""
    real_plan = runs.plan_conversion

    def excluded_b12(directory: Path) -> Any:
        plan = real_plan(directory)

        def fix(row: Any) -> Any:
            if (row.ref.file, row.ref.line) != ("borrowers.csv", 12):
                return row
            return dataclasses.replace(row, disposition=Disposition.EXCLUDED, issues=(
                Issue(Rule.EX_01, "Excluded (injected).", "RECORD_STATUS", "A",
                      (Cause(Rule.EX_01, row.ref),)),
            ))

        return dataclasses.replace(plan, borrowers=tuple(fix(r) for r in plan.borrowers))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runs, "plan_conversion", excluded_b12)
        run_conversion(SAMPLE_DIR, run_id, conversion_root=roots["conversion_root"],
                       evidence_root=roots["evidence_root"])
    reconcile_run(run_id, evidence_root=roots["evidence_root"])


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
    hostile = root / "hostile-extract"
    write_hostile_extract(hostile)
    run_conversion(hostile, HOSTILE_RUN, conversion_root=roots["conversion_root"],
                   evidence_root=roots["evidence_root"])
    run_with_display_cases(CASES_RUN, conversion_root=roots["conversion_root"],
                           evidence_root=roots["evidence_root"])
    convert_with_incomplete_rc12(INCOMPLETE_RUN, roots)
    run_scenario(Scenario.LOADER_DEFECT, DEFECT_RUN, **roots)
    run_scenario(Scenario.CONTROL_TOTALS, CTRL_RUN, **roots)
    engine = create_db_engine(f"sqlite:///{(root / 'los.db').as_posix()}")
    seed_database(engine, PRESETS["small"])
    engine.dispose()
    return roots["evidence_root"]


def make_app(evidence_root: Path):
    return create_app(database_path=evidence_root.parent.parent / "los.db",
                      evidence_root=evidence_root)


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
    harness = HARNESS % {"pages": json.dumps(PAGES), "widths": json.dumps(WIDTHS),
                         "blank_pages": json.dumps(BLANK_PAGES),
                         "badge_pages": json.dumps(BADGE_PAGES)}
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
             "--virtual-time-budget=300000", "--dump-dom",
             f"http://127.0.0.1:{port}/__layout-harness"],
            capture_output=True, text=True, encoding="utf-8", timeout=600, check=False,
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
    assert result["clippedPanels"] == 0, result["clippedBy"]


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
    assert set(links) == {DEFECT_RUN, CTRL_RUN, SHORT_RUN, HOSTILE_RUN, CASES_RUN, INCOMPLETE_RUN}
    for run_id in (DEFECT_RUN, CTRL_RUN):
        link = links[run_id]
        assert link["title"] == link["hiddenText"] == run_id
        assert link["shownText"] == f"{run_id[:14]}…{run_id[-6:]}"
    for run_id in (SHORT_RUN, HOSTILE_RUN, CASES_RUN, INCOMPLETE_RUN):
        assert links[run_id]["shownText"] == run_id and links[run_id]["title"] is None


REPORT_CASES = [(page, width) for page in REPORT_PAGES for width in WIDTHS]


@pytest.mark.parametrize(("page", "width"), REPORT_CASES)
def test_report_identifiers_and_references_never_wrap(
    layout: dict[str, Any], page: str, width: int
) -> None:
    assert case(layout, page, width)["brokenValues"] == []


@pytest.mark.parametrize(("page", "width"), REPORT_CASES)
def test_complete_report_values_open_by_keyboard_without_page_overflow(
    layout: dict[str, Any], page: str, width: int
) -> None:
    result = case(layout, page, width)
    opened = result["opened"]

    assert result["summaries"] > 0
    assert result["focusableSummaries"] == result["summaries"]
    assert opened["values"] == result["summaries"]
    assert opened["visible"] == opened["values"], opened
    assert opened["narrow"] == 0 and opened["clipped"] == 0, opened
    assert opened["copyButtons"] == opened["values"], opened
    assert opened["pageScrollWidth"] <= opened["pageClientWidth"], opened
    assert opened["lastColumnReachable"], opened


@pytest.mark.parametrize(("page", "width"), REPORT_CASES)
def test_source_line_column_is_whole_at_full_scroll(
    layout: dict[str, Any], page: str, width: int
) -> None:
    report = case(layout, page, width)["report"]

    for state in (report["collapsed"], report["expanded"]):
        assert state["problems"] == [], state
        assert state["pageOverflow"] <= 0, state
        rem = state["rem"]
        assert state["padRight"] >= 1.5 * rem - 0.5, state
        assert state["width"] >= min(26 * rem, width - 5 * rem) - 1, state
        assert state["gapToEdge"] >= -1, state


@pytest.mark.parametrize(("page", "width"), REPORT_CASES)
def test_sticky_columns_only_on_wide_screens(layout: dict[str, Any], page: str, width: int) -> None:
    report = case(layout, page, width)["report"]

    if width < 1024:
        assert not report["initialSticky"] and report["sticky"] is None
    elif SHORT_RUN in page:
        assert report["initialSticky"], report


@pytest.mark.parametrize(("page", "width"), REPORT_CASES)
def test_sticky_columns_never_overlap_or_show_through(
    layout: dict[str, Any], page: str, width: int
) -> None:
    sticky = case(layout, page, width)["report"]["sticky"]
    if sticky is None:
        return

    assert sticky["sourceAtEdge"] and sticky["noOverlap"], sticky
    assert sticky["share"] <= 50, sticky
    assert sticky["backgrounds"] == {"dark": True, "light": True}, sticky


@pytest.mark.parametrize(("page", "width"), REPORT_CASES)
def test_keyboard_focus_scrolls_values_into_view(
    layout: dict[str, Any], page: str, width: int
) -> None:
    probes = case(layout, page, width)["report"]["focus"]

    assert probes
    for probe in probes:
        assert probe["focused"] and probe["inView"], json.dumps(probe)


def luminance(color: str) -> float:
    channels = [int(c) / 255 for c in re.findall(r"\d+", color)[:3]]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("page", BLANK_PAGES)
def test_blank_value_indicator_is_readable_and_distinct_in_both_themes(
    layout: dict[str, Any], page: str
) -> None:
    result = layout[f"blanks {page}"]
    assert "error" not in result, result.get("error")

    for theme in ("dark", "light"):
        blanks, literals = result[theme]["blanks"], result[theme]["literals"]
        assert blanks, theme
        for item in blanks:
            assert item["text"] == "Blank value" and item["lines"] == 1 and item["width"] > 0, item
            assert item["border"] == "dashed" and item["fontStyle"] == "italic", item
            assert contrast(item["color"], item["background"]) >= 4.5, (theme, item)
        # A recorded value that reads "Blank value" never looks like the indicator.
        if CASES_RUN in page:
            assert literals, theme
            for item in literals:
                assert item["fontStyle"] == "normal" and item["border"] != "dashed", item
                assert item["fontFamily"] != blanks[0]["fontFamily"], item
    assert result["dark"]["blanks"][0]["color"] != result["light"]["blanks"][0]["color"]


def badges(layout: dict[str, Any], page: str) -> dict[str, list[dict[str, Any]]]:
    result = layout[f"badges {page}"]
    assert "error" not in result, result.get("error")
    return result


@pytest.mark.parametrize("page", BADGE_PAGES)
def test_status_badges_are_readable_in_both_themes(layout: dict[str, Any], page: str) -> None:
    result = badges(layout, page)

    for theme in ("dark", "light"):
        status = [b for b in result[theme] if set(b["classes"]) & set(STATUS_BADGES)]
        assert status, (theme, page)
        for badge in status:
            for background in filter(None, (badge["background"], badge["hovered"])):
                ratio = contrast(badge["color"], background)
                assert ratio >= 4.5, (theme, round(ratio, 2), background, badge)


def rule_badges(layout: dict[str, Any], theme: str) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for page in RULE_RESULT_PAGES:
        for badge in badges(layout, page)[theme]:
            if badge["result"]:
                found.setdefault(badge["result"], []).append(badge)
    return found


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_rule_results_are_distinct_without_relying_on_colour(
    layout: dict[str, Any], theme: str
) -> None:
    found = rule_badges(layout, theme)

    assert set(found) == {"PASS", "FAIL", "INCOMPLETE"}, set(found)
    for result, items in found.items():
        assert {b["text"] for b in items} == {result}, items
        assert len({(b["color"], b["borderStyle"], b["borderColor"]) for b in items}) == 1, items
        for badge in items:
            assert contrast(badge["color"], badge["background"]) >= 4.5, (theme, badge)
    passed, failed, incomplete = (found[r][0] for r in ("PASS", "FAIL", "INCOMPLETE"))
    # Besides its label, INCOMPLETE is the only one outlined, and PASS and FAIL differ in hue.
    assert passed["borderColor"] == "rgba(0, 0, 0, 0)" == failed["borderColor"]
    assert incomplete["borderStyle"] == "solid"
    assert incomplete["borderColor"] != "rgba(0, 0, 0, 0)"
    assert len({passed["color"], failed["color"], incomplete["color"]}) == 3


def test_rule_result_colours_follow_the_theme(layout: dict[str, Any]) -> None:
    dark, light = rule_badges(layout, "dark"), rule_badges(layout, "light")
    for result in ("PASS", "FAIL", "INCOMPLETE"):
        assert dark[result][0]["color"] != light[result][0]["color"], result
        assert dark[result][0]["background"] != light[result][0]["background"], result


def test_wide_desktop_shows_the_run_list_without_scrolling(layout: dict[str, Any]) -> None:
    (region,) = case(layout, "/conversions", 1920)["regions"]
    assert not region["overflowing"], region


def test_mobile_run_list_scrolls_inside_its_region(layout: dict[str, Any]) -> None:
    (region,) = case(layout, "/conversions", 390)["regions"]
    assert region["overflowing"] and region["hintVisible"], region
