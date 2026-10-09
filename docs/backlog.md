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

### CONV-002: Exception, exclusion, and warning reports are not produced

- **Priority:** P2 (functionality gap)
- **Status:** Open
- **Description:** Rejected rows, excluded rows, and warnings appear only as counts and rule
  codes from the manifest's validation summary. The run detail page says the reports "are not
  produced yet". Reviewers cannot see a per-row list with the reason for each row.
- **Acceptance criteria:**
  - Each run writes exception, exclusion, and warning reports into its evidence, listing each
    row's file, line, source key, rule codes, and root cause.
  - The manifest records the reports' checksums.
  - Reconciliation checks that report totals match the validation summary.
  - The run detail page shows or links the reports read-only, with the same size caps as other
    evidence.

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
