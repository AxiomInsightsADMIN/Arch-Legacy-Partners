# Access Layer Guide

This guide is for **Austin's team** after handoff. It covers two paths
to pull data from the wastewater facility database:

1. **Pre-built SQL Views** (Supabase Table Editor) — easiest, point-and-click
2. **Query library** (Supabase SQL Editor) — for ad-hoc questions

The third path — monthly refreshed CSVs — is automatic; you don't need
to run anything for it. See [Monthly CSV refresh](#monthly-csv-refresh)
below.

If you have never used Supabase before, start with section 1. The whole
workflow is **read-only** from your side: nothing you click in the
Table Editor or SQL Editor mutates the database. Click freely.

---

## Opening the project

1. Log in at [supabase.com](https://supabase.com) with the account
   Austin shared during handoff.
2. Open the Arch Legacy Partners project. The left-hand nav shows
   **Table Editor**, **SQL Editor**, **Database**, and other entries.
3. Bookmark the project URL — it includes the unique project ID and
   is shorter than navigating from the org dashboard each time.

---

## 1. Pre-built SQL Views (Table Editor)

The database ships with **20 SQL Views** for the most common
questions. Each view is a saved query with a fixed shape and an
inline description. Use these when you want to look at a slice of the
data and don't want to write any SQL.

### Where to find them

1. Click **Table Editor** in the left nav.
2. In the table list (also on the left), scroll past the tables to
   the **Views** section. You'll see 20 entries starting with `v_`.
3. Click any view. The Table Editor renders the result set just like
   a table — but you can't edit rows (views are read-only).

### The 20 views

| Category | View | What it returns |
|---|---|---|
| Scope | `v_all_in_scope` | Every canonical facility in v1 scope (cross-state) |
| Scope | `v_tx_in_scope` | Texas only |
| Scope | `v_nc_in_scope` | North Carolina only |
| Per-state-per-type (14) | `v_tx_potw_receiving_station` | TX POTW receiving stations |
| | `v_tx_county_manhole_program` | TX county manhole programs |
| | `v_tx_land_application_site` | TX land application sites |
| | `v_tx_private_regional_septage_facility` | TX private/regional septage |
| | `v_tx_composting_facility` | TX composting facilities |
| | `v_tx_anaerobic_digester` | TX anaerobic digesters |
| | `v_tx_transfer_station` | TX transfer stations |
| | `v_nc_potw_receiving_station` | NC POTW receiving stations |
| | `v_nc_county_manhole_program` | NC county manhole programs |
| | `v_nc_land_application_site` | NC land application sites |
| | `v_nc_private_regional_septage_facility` | NC private/regional septage |
| | `v_nc_composting_facility` | NC composting facilities |
| | `v_nc_anaerobic_digester` | NC anaerobic digesters |
| | `v_nc_transfer_station` | NC transfer stations |
| Acceptance (3) | `v_accepts_septage` | Facilities that explicitly accept septage |
| | `v_accepts_grease_trap` | Facilities that explicitly accept grease trap waste |
| | `v_accepts_portable_toilet` | Facilities that explicitly accept porta-potty waste |

The three **acceptance** views will be **mostly empty** until Phase 4
enrichment lands and the `accepts_*` columns get populated. That's
expected behavior, not a bug — by design, a facility doesn't get an
acceptance flag unless we found explicit text evidence.

### Exporting a view to CSV

1. Open the view (click it in the sidebar).
2. Click the **Export** dropdown at the top right of the table.
3. Choose **Export CSV**.
4. The browser downloads the full result set as `<view_name>.csv`.

This works for any view or table; there's no separate "Export to
Excel" — open the CSV in Excel, Numbers, or Google Sheets.

### Filtering before export

The Table Editor has a **Filter** button (next to Export). Click it
to add per-column filters without writing SQL. The export reflects
the filtered result, not the full view. Examples:

- `v_nc_transfer_station` → filter `county = 'Wake'` → export the
  Wake County subset only
- `v_all_in_scope` → filter `state = 'TX'` AND
  `facility_type = 'composting_facility'` → equivalent to
  `v_tx_composting_facility` but with one-click filters you can adjust

---

## 2. Query library (SQL Editor)

For questions the views don't directly answer, the project ships
with a small library of named queries at `db/queries/` in the
repo. Each file is a single SQL statement with a header comment
explaining purpose, parameters, and output columns.

### Running a query from the library

1. Open the repo at <https://github.com/AxiomInsightsADMIN/Arch-Legacy-Partners>.
2. Navigate to `db/queries/`.
3. Click the file you want, e.g. `facilities_by_county.sql`.
4. Copy the SQL.
5. Back in Supabase, click **SQL Editor** in the left nav.
6. Click **+ New query** (top right).
7. Paste the SQL.
8. Click **RUN** (or `Ctrl+Enter` / `Cmd+Enter`).
9. The result appears below the editor. Use the **Download CSV**
   button (top right of the result panel) to export.

### The 9 queries

| File | What it answers |
|---|---|
| `all_tx_septage_facilities.sql` | "Who handles septage in Texas?" |
| `all_nc_transfer_stations.sql` | "Where are the NC transfer stations?" |
| `facilities_by_county.sql` | Coverage / concentration: count per (state, county, type) |
| `facilities_missing_coords.sql` | Data-quality: facilities without lat/lng |
| `facilities_with_low_confidence_geocoding.sql` | Data-quality: addresses the geocoder mismatched on state |
| `canonical_history_recent.sql` | Audit: field-level changes in the last 30 days (post-Phase-4) |
| `hold_review_queue.sql` | Resolver borderline matches (score 75–91) for human review |
| `per_source_row_counts.sql` | Operational: raw row counts per source vs signature |
| `duplicate_candidates.sql` | Likely duplicate canonicals by name+state+county |

### Modifying a query

The library files are **starting points**, not the only way to query.
Once a query is open in the SQL Editor you can:

- Add `WHERE` clauses to filter further
- Change `ORDER BY` to sort differently
- Add `LIMIT N` for a quick sample
- Save the modified query (click **Save** in the SQL Editor — it
  saves to *your* SQL Editor history, not back to the repo)

Saved-to-Supabase queries appear in your SQL Editor history pane on
the left, scoped to your account. They don't affect anyone else's
view.

---

## 3. Monthly CSV refresh

Every month on the 1st at 09:00 UTC, GitHub Actions runs the full
pipeline (scrape → resolve → export) and opens a PR on the repo
with the refreshed CSVs. You don't need to run anything.

### Where the refreshed CSVs land

- A new branch `refresh/YYYY-MM-DD` (e.g. `refresh/2026-06-01`) is
  created automatically.
- The branch contains updated `exports/facilities_primary.csv` and
  `exports/facilities_provenance.csv`, plus
  `exports/drift_report.json`.
- A PR titled `Monthly refresh 2026-06-01` opens against `main`. You
  review the diff and merge when it looks right.

### What to check on the PR

1. **CI status**: all checks pass (ruff lint, ruff format, pytest,
   schema migration).
2. **`exports/drift_report.json`**: at the top, `overall_status`
   should be `"pass"`. If it's `"pause"`, the resolver did **not**
   run — one of the source scrapers detected suspicious drift
   (per locked decision 8.7) and halted the workflow before the
   canonical rebuild. Read the per-source `reason` field to find
   which source and why. Address before merging.
3. **Diff size on `facilities_primary.csv`**: typical month-to-month
   diff is small (~tens to low-hundreds of row changes). A diff
   spanning thousands of rows means real upstream churn — open
   `drift_report.json` and the latest source build_log entries to
   investigate before merging.
4. **Email alert (when configured)**: if a step fails before the PR
   opens, the operator gets an email with a link to the failing
   workflow run. The SMTP-secrets setup is in
   `docs/build_log.md` under "Phase 6 design notes (pin: SMTP
   secrets handoff)".

---

## 4. Direct database access (advanced)

If you need direct psql or DBeaver / DataGrip access:

- Connection string: from the Supabase project page → **Project
  Settings** → **Database** → **Connection string**. Use the
  **Session pooler** entry (host `aws-1-ap-northeast-1.pooler.
  supabase.com`, port 5432, user `postgres.<project-ref>`).
- Read-only role: ask the project owner to provision one if you want
  a read-only credential separate from the service-role key.
- For one-off scripts, the project's `.env.example` lists the
  variable names the codebase expects; mirror those in your local
  `.env` and use the same psycopg2 connection helper the loaders use
  (see `scrapers/_loader_utils.db_connect`).

---

## 5. Common questions

**"My export looks short — is there filtering I'm missing?"**
Most likely. The `v_*_in_scope` views are intentionally filtered to
the 2,363 typed canonicals. If you want everything including the
~70K ECHO industrial NPDES rows, query the underlying table
directly: `SELECT * FROM canonical_facility` (no view).

**"Can I add a column to a view?"**
Yes, but it requires a migration. Open an issue describing the
column you want; the project owner can add it to
`supabase/migrations/`.

**"How fresh is the data?"**
The "first_seen_at" and "last_seen_at" columns on every facility
tell you when the resolver last touched that canonical row. The
monthly cron refreshes all rows; off-cycle changes happen during
manual Phase 4 enrichment runs.

**"I see a duplicate facility — what do I do?"**
Run `duplicate_candidates.sql`, find the cluster, and decide
whether the rows are truly the same business. If yes, file an issue
with the cluster's canonical IDs; Phase 4 will merge or flag during
the next enrichment pass.

---

## 6. Where to ask

- Repo: <https://github.com/AxiomInsightsADMIN/Arch-Legacy-Partners>
- Issues: <https://github.com/AxiomInsightsADMIN/Arch-Legacy-Partners/issues>
- Project owner contact + the API-key rotation runbook live in
  `docs/build_log.md` under "Phase 6 design notes".
