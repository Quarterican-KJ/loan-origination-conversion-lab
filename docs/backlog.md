# Backlog

The permanent issue and improvement backlog for Loan Origination & Conversion Lab. Keep it
lightweight: one entry per issue, edited in place, never deleted. When an item is resolved, move
it to [Resolved Issues](#resolved-issues).

## Workflow

1. **Log it when you find it.** Add an entry with the next free ID in its area. IDs are stable:
   never reuse or renumber them.
2. **Classify its severity.**
   - **P1:** correctness, security, or data integrity.
   - **P2:** usability problems or meaningful functionality gaps.
   - **P3:** lower-impact enhancements.
3. **Define acceptance criteria.** Write checkable statements that say when the item is done,
   including the tests that prove it.
4. **Resolve urgent issues before publication.** Fix any open P1 issue in scope for a release,
   or explicitly defer it with a reason, before publishing to GitHub.
5. **Update the status.** Mark an item Resolved only once it is tested and committed, and note
   the change. Until then, keep the status as pending, e.g. "Resolved, pending publication".

| Area | Prefix |
|---|---|
| Web interface | `UI-` |
| Conversion pipeline and evidence | `CONV-` |
| Security and access control | `SEC-` |
| Architecture, scale, and operations | `ARCH-` |

Statuses: **Open** (a known problem), **Planned** (agreed enhancement, not started),
**In progress**, **Deferred** (intentionally postponed, with a reason), and **Resolved**.

## Open Issues

### CONV-001: Planner and reconciler share disposition logic

- **Priority:** P1 (correctness)
- **Status:** Open
- **Description:** Reconciliation decides each row's disposition by re-running the Phase 1
  validation rules. A planner defect that wrongly rejects a valid row therefore reconciles cleanly
  (the row is neither expected nor present), and rule codes and root causes on rejected rows are
  not compared. For the sample extract, `tests/test_conversion_spec_acceptance.py` guards this
  with hand-transcribed constants; for any other extract it is a reviewer responsibility. See
  "Independence limits" in [architecture.md](architecture.md).
- **Acceptance criteria:**
  - An independent business eligibility review decides which rows should load, without calling
    the planner's validation code.
  - Reconciliation compares the planner's dispositions, rule codes, and root causes against that
    review and fails on any disagreement.
  - A test injects a planner defect that wrongly rejects a valid row, and reconciliation fails
    it with a specific discrepancy.
  - The existing scenarios and spec acceptance tests still pass.

### CONV-003: Conversion database integrity is not verified from the web interface

- **Priority:** P2 (functionality gap; the integrity check exists on the command line)
- **Status:** Open
- **Description:** Conversion Management shows the conversion database checksum as recorded.
  Only `verify_reconciliation` re-hashes the database, so database drift is invisible in the web
  interface.
- **Acceptance criteria:**
  - The run detail page offers a read-only, on-request integrity check that re-hashes the
    database without opening it as SQLite and without writing anything.
  - The result is labeled clearly as Matches, Differs, Too large, or Missing.
  - Size limits and streamed hashing keep the check bounded.
  - Tests cover a matching database, a modified database, and a missing database.

### UI-006: Report tables were hard to read, and long tables hard to scroll sideways

- **Priority:** P2 (usability; data was hard to read but not altered)
- **Status:** In progress. Manual acceptance on Windows 11 failed on 2026-10-10 because the
  browser ran outdated cached assets (see "Acceptance failure diagnosis" below); fixed and
  awaiting a repeat manual check. Not yet committed or pushed.
- **Description:** On `/conversions/{run_id}/reports/exceptions` (and the exclusion and warning
  pages), the Key and Unit identifiers wrapped one character per line. Raw source lines were
  squeezed until unreadable, and root-cause references broke inside file names. The cause was
  `overflow-wrap: anywhere` on the report cells, which let the table shrink those columns to
  one character wide.
- **Fix:**
  - Identifiers stay on one line in monospace, with exact spacing and leading zeros. A value
    over 24 characters shows a preview that opens, in a native `<details>` disclosure, to the
    complete value.
  - Every source line shows a one-line preview of up to 40 characters and opens to the
    complete, untruncated line, which wraps inside a fixed-width box. A Copy button is added
    where the browser's clipboard API is available.
  - Root-cause references (`RULE file:line`) never break inside; the cell wraps only between
    references.
  - `.table-scroll` is now a positioned container, so visually hidden text inside a table can no
    longer widen the page.
  - Follow-up correction (same day): at full horizontal scroll, the Source line column was
    cramped against the table edge, and the record context (Source and Key) scrolled out of
    view.
    - The Source line column now has a fixed width of `min(26rem, 100vw - 5rem)` (364px at
      768px and wider, 320px at 390px) with 1.5rem right padding. Its preview and expanded
      value are sized to fit inside it.
    - On screens at least 1024px wide, Source and Key stay visible as sticky columns with opaque
      backgrounds, but only while they take at most half the scroll region. Mobile and tablet
      keep the full scroll width.
    - A focused control inside a table is scrolled fully into view, clear of the sticky
      columns.
  - Filters, downloads, evidence, escaping, themes, and conversion logic are unchanged.
- **Acceptance criteria (met):**
  - No identifier or reference wraps inside, and the page never scrolls sideways, at 390, 768,
    1280, and 1920px, with every disclosure closed or open.
  - At initial and full scroll, collapsed and expanded, the whole Source line cell and its
    contents are in view: not cut off, not under the sticky columns, and with its right padding.
  - Sticky columns appear only from 1024px, never overlap, take at most half the region, and
    match the panel background in both themes. Measured: 40% of the region at 1280px and 30% at
    1920px on Exceptions, 24% and 18% on Warnings, and 46% and 34% with long hostile keys.
  - Keyboard focus on a Source line or Unit value brings it fully into view.
  - Every column stays reachable in its scroll region.
  - The complete values match the archived report exactly, including a 2,568-character hostile
    source line.
  - Markup in values is escaped.
  - Every summary takes keyboard focus.
  - `tests/test_report_readability.py` and the report pages added to `tests/test_web_layout.py`
    cover this. The layout checks fail when the old wrapping rule is restored.
- **Application-wide horizontal navigation (follow-ups, 2026-10-09 to 2026-10-10):** a table's
  native horizontal scrollbar sits at the table's bottom and may auto-hide. On a long table,
  users reviewing the top or middle rows could not scroll sideways.
  - The first correction, a conditional "scroll dock", failed user acceptance. Its track was a
    native scrollbar that could still auto-hide, and the dock hid itself whenever the table's
    bottom was in view, leaving only the native scrollbar. It has been removed, with its window
    scroll and resize listeners, its disclosure listener, and its visibility rules.
  - Every table in the application sits in the shared `.table-scroll` region. The LOS
    Dashboard, Applications, and Application Details tables were wrapped and labeled; the
    conversion pages already used it.
  - Replacement (2026-10-10): `app.js` wraps each region in a `.table-frame` that ends with the
    table's own scrollbar.
    - The scrollbar has Scroll left and Scroll right buttons and a drawn track and thumb. It
      does not use the platform's native scrollbar, and the table's native scrollbar is hidden
      while it is present.
    - It is shown whenever the table overflows sideways and hidden otherwise. It does not
      depend on scroll position, pointer movement, idle time, or focus.
    - It is sticky at the bottom of its frame. It sits under the table's last row when that row
      is in view, and otherwise pins to the bottom of the viewport. It never leaves its own
      table's area.
    - Active table: the table crossing the bottom edge of the viewport has its bar pinned
      there. Every other table's bar sits under its own last row, so bars never stack or
      overlap.
    - The thumb can be dragged with a mouse, pen, or touch. Pressing the track moves 80% of
      the table's width toward the press, as the buttons do.
    - The thumb is a focusable `role="scrollbar"` control, labeled "{table} columns", with
      `aria-controls` and `aria-valuenow` from 0 to 100. Arrow keys move it 40px; Page Up,
      Page Down, Home, and End also work.
    - Horizontal wheel or trackpad movement over the bar scrolls the table; vertical movement
      still scrolls the page. Any scroll of the table itself (wheel, trackpad, touch, keyboard,
      or focus) moves the thumb.
    - A single ResizeObserver on each region, table, and track keeps the bar current on
      resize, responsive layout changes, and opened disclosures.
  - `.panel` clips with `overflow: clip`, so the bar can stick inside it. Focusing a control
    hidden behind a pinned bar scrolls the page to reveal it.
  - Timestamps in run summaries may now wrap. At 390px, "Oct 10, 2026 · 12:47 PM UTC" was
    2px wider than its panel and was clipped.
  - Tested in `tests/test_table_scrollbar.py`.
    - Chrome is driven through the DevTools Protocol, whose Input domain sends trusted mouse,
      wheel, touch, and key events. Chrome runs with `--hide-scrollbars`, so no native scrollbar
      is ever drawn.
    - It covers all 11 pages with tables at 390, 768, 1280, and 1920px.
  - Measured:
    - The bar is 35.4px high in every case. It is drawn, opaque, on top, and inside the viewport
      when each overflowing table is placed at the top, middle, and bottom of the viewport.
    - It stays drawn after 1 second idle with the pointer moved away, after clicks, and after
      focus changes.
    - It is pinned to the viewport bottom on long tables: Applications (100 rows) at 390 and
      768px, and the Exceptions reports at all four widths.
    - Thumb-to-track contrast is 5.5:1 in the light theme and 5.7:1 in the dark theme.
    - A real mouse drag of 29–80px moved the thumb exactly that far and scrolled the table to
      within 1px of the expected position, for example 551px for an 80px drag on Exceptions at
      390px.
    - Track presses moved the table toward the press on every table with room to press.
    - Wheel input of 120px, a touch drag on the thumb, and a touch swipe on the rows all
      scrolled the table and moved the thumb to within 1px.
    - Tab moves from the table to Scroll left, then the thumb, then Scroll right, each with a
      2px focus ring. End reaches the last column; with sticky Source and Key columns from
      1280px, they stay in place.
    - Opening the 23 hostile source-line disclosures widened the range from 1,175 to 1,593px
      at 1280px. The thumb resized and End still reached the last column.
    - On pages with two to five overflowing tables, dragging one thumb moved only that table.
    - Resizing between 390, 1920, and 1280px showed and hid bars correctly, with exactly one
      bar per table.
    - The page never scrolls sideways.
  - Breaking the implementation on purpose (bar not sticky, drag not scaled) failed 72 of these
    tests. The full suite passes (1,548 passed, 2 skipped).
  - Pending manual verification:
    - a desktop browser with overlay or auto-hiding scrollbars (Windows 11, macOS);
    - a physical touch device.
  - Limitations:
    - Automated touch input is emulated by Chrome, not a physical screen.
    - Keyboard toggling of disclosures is not automated.
    - While pinned, the bar covers about 35px of its own table's rows. They come into view as
      the page scrolls, or when one of their controls is focused.
    - `overflow: clip` needs Safari 16 or later. In older browsers the bar sits under the
      table without pinning.
    - Tables inserted after the page loads get no bar.
- **Acceptance failure diagnosis (2026-10-10):** on Windows 11, the horizontal scrollbar still
  disappeared once vertical scrolling stopped.
  - Cause: the browser never ran the persistent scrollbar.
    - Static files were served with no `Cache-Control` header, so Edge applied heuristic
      freshness and reused its cached `app.js` and `app.css` without contacting the server.
    - Evidence: the dev server log shows the 13:00 page load requesting only the HTML. Edge's
      code cache held `app.js` from 12:28, while the new files were written at 12:46 and 12:52.
      No service worker is involved, and the server itself served the current files.
  - The cached files were the earlier scroll dock, whose track is a native scrollbar.
    - Reproduced in a visible Edge 154 window at 1685×869 CSS px (2560×1440 at 150%), with
      overlay scrollbars enabled as on Windows 11.
    - With the dock assets, the dock's ‹ and › buttons showed but the track drew nothing: a
      brightness range of 0 across the track, right after scrolling and after 3 seconds of
      idle.
    - With classic scrollbars, the same track drew a scrollbar (range 36).
    - The current custom bar drew its thumb in both modes (range 130 after scrolling and after
      idle). Its state stayed shown, pinned, opaque, and on top in every 100ms sample.
    - The sticky placement worked in the real browser, so no viewport-fixed control was
      needed.
  - Fix:
    - Static files are served with `Cache-Control: no-cache`, so browsers revalidate every use;
      unchanged files return 304.
    - Pages request assets as `?v=<content hash>`, so a changed file gets a new URL even in a
      browser holding an old copy.
    - The scrollbar implementation is unchanged.
  - Verified in visible Edge with overlay scrollbars against the running dev server.
    - The first load fetched `app.css?v=60be624f735f`, `theme.js?v=2f6bc5644914`, and
      `app.js?v=d0621eb55ecc` with `no-cache`; a reload revalidated all three (304).
    - After real wheel scrolling and 3.25 seconds of idle, all 13 samples showed the bar drawn,
      pinned, opaque, and on top.
  - Regression tests:
    - `tests/test_table_scrollbar.py` turns on Chrome's overlay scrollbars and scrolls with real
      wheel input to mid-table. It then requires the bar to stay drawn and pinned in samples
      every 0.25 seconds through 3.2 seconds of idle. It also checks that hiding the thumb
      changes the screenshot, proving the thumb paints pixels. It runs on Exceptions at
      1707×869 in both themes, at 390px, on the hostile report at 1280px, and on Applications
      at 768px.
    - A thumb that fades after 1 second and a transparent thumb each failed it.
    - `tests/test_web.py` checks the cache header, 304 revalidation, and versioned asset URLs.
  - The full suite passes (1,558 passed, 2 skipped).
  - Next manual check: reload the page in Edge once. The page now requests the new versioned
    assets, so no cache clearing is needed. Then repeat the vertical scroll and idle test.

## Planned Enhancements

### UI-001: Conversion run sorting and filtering

- **Priority:** P2 (usability)
- **Status:** Planned
- **Description:** The run list shows at most 200 runs, most recently modified first, with no
  sorting, filtering, or pagination.
- **Acceptance criteria:**
  - Runs can be filtered by status, evidence condition, and run ID prefix (e.g. `SYN-`).
  - Runs can be sorted by started time, finished time, and status.
  - Filters and sort order are kept in the query string, so views can be bookmarked.
  - Invalid filter values are ignored safely.
  - The 200-run display cap is still enforced and is stated when reached.

### UI-002: Borrower detail pages

- **Priority:** P2 (functionality gap)
- **Status:** Planned
- **Description:** Borrowers appear only as rows on application pages. There is no page showing
  one borrower and every application they are party to.
- **Acceptance criteria:**
  - `GET /borrowers/{id}` shows the borrower's details and the applications they are party to,
    with their role on each.
  - Application pages link to the borrower page.
  - Unknown IDs return 404.
  - The page is read-only and passes the existing accessibility and layout checks.

### UI-003: Global search and navigation

- **Priority:** P2 (usability)
- **Status:** Planned
- **Description:** The header search only searches applications, through `/applications?q=`.
  It cannot find a conversion run by ID or jump directly to an application number.
- **Acceptance criteria:**
  - One search finds applications, borrowers (once UI-002 exists), and conversion runs by ID.
  - Results are grouped by type.
  - An exact application number or run ID goes straight to its page.
  - Input length and result counts stay capped.
  - The search is usable by keyboard alone.

### UI-004: Navigation between related collateral records

- **Priority:** P3 (enhancement)
- **Status:** Planned
- **Description:** Collateral is shown inside each application. When collateral is pledged to
  several applications, there is no way to move from one application to the others it secures.
- **Acceptance criteria:**
  - Each collateral item lists the other applications it is pledged to, with links.
  - Optionally, a collateral detail page shows the item's pledges and liens.
  - Shared collateral in the seed data is covered by a test.

### CONV-004: Scenario metadata visibility

- **Priority:** P3 (enhancement)
- **Status:** Planned
- **Description:** Synthetic scenario runs are identified only by their `SYN-` prefix. The web
  pages do not read `data/scenarios/<run_id>/scenario.json`, so the scenario name, the injected
  defect, and the expected outcome are not shown.
- **Acceptance criteria:**
  - The run detail page labels scenario runs and shows the scenario name, expected outcome, and
    any injected defect from `scenario.json`.
  - The file is read with the same size caps and parsing safeguards as other evidence.
  - A missing or malformed descriptor shows as "Not available".
  - Production runs are never labeled as scenarios.

## Deferred Architectural Work

### SEC-001: Authentication and authorization

- **Priority:** P1 (security)
- **Status:** Deferred. The interface is read-only and runs locally. This must be done before
  any write, upload, or approval feature is added.
- **Description:** The web interface has no authentication or roles. That is acceptable only
  while every route is read-only.
- **Acceptance criteria:**
  - Every route requires an authenticated user.
  - Roles at least separate viewers, operators, and approvers.
  - Write routes, when added, check authorization on the server and are protected against
    cross-site request forgery.
  - Security headers, including a Content-Security-Policy, are set.
  - Tests show that unauthenticated and unauthorized requests are refused.

### SEC-002: Release approval and segregation of duties

- **Priority:** P1 (data integrity and control)
- **Status:** Deferred. Depends on SEC-001.
- **Description:** A `RECONCILED` run is ready for review, not released. No release workflow
  exists, and nothing prevents one person from running and approving the same conversion.
- **Acceptance criteria:**
  - Release requires a `RECONCILED` run that passes `verify_reconciliation` at approval time.
  - Release requires an approver who is not the person who ran the conversion.
  - The approver's identity, time, and verified checksums are recorded in the evidence.
  - Release and decline are never automatic.
  - Tests cover self-approval being refused and a stale or modified run being refused.

### ARCH-001: Large-dataset reconciliation performance and concurrency safety

- **Priority:** P2 (scale)
- **Status:** Deferred. Current runs use the small sample extract.
- **Description:** Reconciliation and evidence reading are sized for small extracts. Two
  processes working on the same run ID at once are not coordinated beyond refusing to reuse an
  existing run directory.
- **Acceptance criteria:**
  - Reconciliation of a large synthetic extract (e.g. 100,000 applications) completes within a
    documented time and memory budget.
  - Concurrent operations on one run are serialized or refused with a clear error, for example
    through a per-run lock.
  - Tests cover two simultaneous reconciliations of the same run.

### ARCH-002: Evidence retention and tamper resistance

- **Priority:** P1 (data integrity)
- **Status:** Deferred. Evidence lives in local, git-ignored folders.
- **Description:** Evidence is protected by checksums recorded in the manifest. Anyone who can
  write to the folder can still replace both a file and its recorded checksum, and no retention
  policy prevents evidence from being deleted.
- **Acceptance criteria:**
  - Evidence is sealed by something outside the run folder, such as a signed or append-only
    record of checksums.
  - Verification detects a coordinated change to a file and its manifest.
  - A documented retention period applies, and deletion before it ends is refused or recorded.
  - Tests show that a re-signed or swapped manifest is detected.

## Resolved Issues

### UI-007: Blank report values looked like a rendering problem

- **Priority:** P3 (enhancement; display only)
- **Status:** Resolved, pending publication. Tested on 2026-10-10; not yet committed or pushed.
- **Description:** In the Exceptions, Exclusions, and Warnings reports, a field whose recorded
  value is empty or only whitespace showed nothing after its label (for example `LAST_NAME:`).
  The same applied to empty Expected and Actual values in reconciliation discrepancies.
- **Fix:**
  - A shared `blank` template test (`web/formatting.py`) and `blank_value` macro
    (`_conversion.html`) show such values as a "Blank value" label: italic, in a dashed outline,
    in the muted text color of each theme. Its tooltip says whether the value is empty or how
    many whitespace characters it holds.
  - `raw_value`, which renders every report value, uses it, so all three report pages share one
    path. The discrepancy table uses it for Expected and Actual.
  - Missing or non-applicable values keep their existing `—` or "Not available". Recorded text
    that reads "Blank value", `0`, and `false` show as ordinary monospace values. All text stays
    HTML-escaped.
  - The archived reports, downloads, search, filters, reconciliation, conversion rules, and
    databases are unchanged. Application pages show validated LOS data and were left as they
    are.
- **Acceptance criteria (met):**
  - Empty and whitespace-only values show "Blank value"; missing values, zero, `false`, and the
    literal text "Blank value" do not.
  - Searching "Blank value" finds only rows whose recorded text says so, and downloaded CSVs
    match the archived reports.
  - `tests/test_blank_values.py` covers the cases above, HTML-sensitive values, all three report
    pages, and the discrepancy table. `tests/test_web_layout.py` checks in headless Chrome or Edge
    that the label is legible (contrast of at least 4.5:1) and distinct from values in the dark
    and light themes. The full suite passes (1,605 passed, 2 skipped).

### CONV-002: Exception, exclusion, and warning reports are not produced

- **Priority:** P2 (functionality gap)
- **Status:** Resolved, pending publication. Tested on 2026-10-09; not yet committed or pushed.
- **Description:** Rejected rows, excluded rows, and warnings appeared only as counts and rule
  codes from the manifest's validation summary. The run detail page said the reports "are not
  produced yet". Reviewers could not see a per-row list with the reason for each row.
- **Fix:**
  - New runs write `reports/exceptions.csv`, `exclusions.csv`, and `warnings.csv` from the
    validated plan, before any load (spec section 13.1). Each row has the file, line, source key,
    unit key, stage, rule, dependent flag, root cause, field, exact source value, message,
    remediation, and raw source line.
  - The reports are checked against the plan's row accounting before anything is written, then
    written atomically and read back. The manifest (version 3) records their state, SHA-256,
    size, and counts. A failure makes the run `FAILED` at stage `reports` with incomplete
    evidence, before any database exists (command-line exit code 11).
  - Conversion Management has a Reports tab with searchable, filterable report pages and a
    spreadsheet-safe CSV download, served only when the file verifies.
  - `DEMO-001`, the scenario runs, the conversion rules, and the LOS models are unchanged.
- **Acceptance criteria (met):**
  - Each new run writes the three reports into its evidence, listing each row's file, line,
    source key, rule codes, and root cause. The sample extract gives 18 exceptions, 6
    exclusions, and 3 warnings.
  - The manifest records the reports' checksums and counts.
  - Readiness for reconciliation (`check_ready`, used by `reconcile_run`) refuses a run whose
    reports are missing, changed, or whose counts disagree with the validation summary. This
    replaces the original wording, "Reconciliation checks that report totals match the
    validation summary"; reconciliation does not re-derive the report contents.
  - The run detail page links the reports read-only, with a 16 MiB size cap and a 500-row
    display cap.
  - `tests/test_conversion_reports.py` covers exact contents, dependent failures, warnings,
    manifest integrity, interrupted writes, path safety, HTML escaping, CSV safety, and no-write
    web behavior. The full suite passes (844 passed, 2 skipped).

### UI-005: Conversion tables clipped their rightmost columns

- **Priority:** P2 (usability; data was hidden but not altered)
- **Status:** Resolved, pending final GitHub publication. Tested on 2026-10-09; not yet
  committed.
- **Description:** On narrower windows, the `/conversions` run table cut off its rightmost
  columns. Its natural width was about 1,720px. Several tables on the run detail and
  reconciliation pages sat outside any scroll container, so their panels clipped them.
- **Fix:**
  - Every Conversion Management table now sits in a labeled, horizontally scrollable region.
  - Edge shadows show when more columns are off screen. Only while a table overflows, a hint
    appears and the region becomes focusable for arrow-key scrolling.
  - Long run IDs are shortened on screen; the full ID stays in the tooltip, in screen-reader
    text, and in the link.
  - Selected cells now wrap, which brought the run table down to about 1,250px. It fits
    without scrolling at 1920px.
  - Evidence, conversion logic, and financial formatting are unchanged.
- **Acceptance criteria (met):**
  - No page-level horizontal scrolling at 390, 768, 1280, and 1920px.
  - Every column can be reached by scrolling, by keyboard, and on touch.
  - The hint and focusability appear only when needed.
  - Table headers are scoped.
  - `tests/test_web_layout.py` checks this in headless Chrome or Edge; the full suite passes
    (788 passed, 2 skipped).
