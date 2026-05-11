# Build Log

Daily status entries per the kickoff brief, section 13. Newest entries first.
Each entry covers: what completed, what is in-progress, what is blocked,
decisions made, deviations from the brief (and why).

---

## 2026-05-11 — Day 2 step 3 (EPA CWNS 2022 loader via Playwright)

**Completed**
- **CWNS loader** at `scrapers/federal/epa_cwns.py` using Playwright +
  Chromium headless. Successfully drove the APEX flow that the step-2
  spike said couldn't be done with pure HTTP. Loaded TX and NC per-state
  CWNS data ZIPs and upserted **3,132 raw rows** into
  `raw_facility_record` (TX=2,312 + NC=820). Sequential per state,
  matching the spec.
- **Shared loader utilities** extracted into `scrapers/_loader_utils.py`:
  `db_connect`, `get_source_id`, `begin_run`, `finish_run`,
  `write_signature`, `hash_payload`. Both the ECHO and CWNS loaders now
  import from this single source of truth; any future loader does the
  same. The ECHO loader was refactored in place (no behavior change).
  Both modules also include a `sys.path` shim so `python
  scrapers/federal/X.py` works without needing `python -m`.

**Playwright flow that works**

The step-2 spike correctly identified that the per-state download requires
an APEX session, a state selection, and a survey form submission. The
loader executes that flow end-to-end:

1. `chromium.launch(headless=True)` (committed default per Ryan).
2. `page.goto("/ords/sfdw_pub/r/sfdw/cwns_pub/data-download")`.
3. `page.select_option("#P5_STATE", "<state>")` — onchange triggers a
   full-page navigation to `f?p=148:5:<session>::NO::P5_STATE:<state>`.
   We wait inside `page.expect_navigation()` to catch the reload.
4. Click the first `"Download CSVs"` button on the page (per-state
   section; the page also has a nationwide-dataset button further down).
5. The button is wired to `apex.theme42.dialog('/download-popup?...')`,
   which renders a modal jQuery-UI dialog containing an `<iframe
   src="/download-popup?p2_location_id=<state>&p3_type=State&session=…&cs=…&dialogCs=…">`.
6. Inside that iframe is a survey form titled
   *"Help us learn more about who uses our data"* with a
   `<select id="P3_QUESTION">` of 6 options:
     - `Prefer not to answer`
     - `Federal/State/Local Government`
     - `Researcher`
     - `Industry or NGO`
     - `Other not listed`
   The "Download" button (`button#B5432...`) is rendered with
   `disabled` until a P3_QUESTION value is selected.
7. We select **"Industry or NGO"** — the most accurate description of
   an Axiom-Insights-contracted DB build for a private-sector client
   (Arch Legacy Partners). Decision rationale documented here so future
   maintainers don't have to re-litigate the survey answer. Other
   defensible choices were "Prefer not to answer" (less informative to
   EPA, but technically truthful) and "Other not listed" (vague). The
   only choice we'd refuse is one that misrepresents — none of the
   options do.
8. The Download button becomes enabled. Click it. Playwright's
   `page.expect_download()` catches the resulting browser download.
9. Save zip to `local/cwns_downloads/<state>_<suggested>.zip` for
   forensic retention.

**Validation pipeline (every load runs all four)**

- ZIP magic: first 4 bytes must be `PK\x03\x04`.
- Required members: `FACILITIES.csv` must be present (the spine).
- Row counts: every member CSV must have at least one data row (header
  + ≥1 row); `FACILITIES.csv` must be ≥1 row or the run fails.
- Uniqueness: every `CWNS_ID` in `FACILITIES.csv` is unique (or the run
  fails with a duplicate report).

**Payload shape**

One row per `CWNS_ID` in `raw_facility_record`. `raw_payload` is a JSONB
dict keyed by source table name. 1:1 tables (e.g. `FACILITIES`,
`PHYSICAL_LOCATION`) store a single nested dict; 1:N tables (e.g.
`FACILITY_TYPES` when multiple types apply) store a list of dicts.
Reference tables (`REF_FACILITY_TYPES`) and tables without a `CWNS_ID`
column are not ingested into the per-row payload — they would belong
elsewhere if we needed them later.

**Counts (matches the in-zip CSV totals exactly)**

| | TX | NC | Total |
|---|---:|---:|---:|
| ZIP bytes | 416,605 | 200,474 | 617,079 |
| FACILITIES.csv data rows | 2,312 | 820 | 3,132 |
| `CWNS_ID`s parsed | 2,312 | 820 | 3,132 |
| `raw_facility_record` inserted | 2,312 | 820 | 3,132 |
| updated | 0 | 0 | 0 |
| unchanged | 0 | 0 | 0 |
| cross-state rows (PHYSICAL_LOCATION.STATE_CODE) | 0 | 0 | 0 |
| Elapsed (Playwright + parse + upsert) | 18.6 s | 14.9 s | 33.5 s |

Schema hash identical across both states: `cb282fec57ee…`.

**Cross-state finding (different from ECHO)**

CWNS per-state ZIPs are **geographically pure** — every row's
`PHYSICAL_LOCATION.STATE_CODE` matched the queried state. Zero
cross-state rows for both TX and NC. This is a meaningful contrast with
ECHO's jurisdictional filter (which produced 11 cross-state rows across
the two pulls). For Phase 3, the resolver's state-coverage filter still
applies — but CWNS's contribution should be 0 rows for `STATE_CODE NOT
IN ('TX','NC')`. The filter is still required structurally so future
sources don't bypass it.

**Operational metadata on Supabase**

```
scraper_run rows for epa_cwns_2022:
  run_id=3  status=success  rows_in=2,312  inserted=2,312  err=None
  run_id=4  status=success  rows_in=  820  inserted=  820  err=None

source_signature rows for epa_cwns_2022:
  run=3  http=200  bytes=416,605  schema_hash=cb282fec57ee…  rows=2,312
  run=4  http=200  bytes=200,474  schema_hash=cb282fec57ee…  rows=  820
```

**Table population across the 3,132 CWNS_IDs** (informational; Phase 3
resolver will pick which tables to read per canonical field):

| Always populated (3,132 / 3,132) | Mostly populated | Sparse |
|---|---|---|
| `FACILITIES`, `FACILITY_TYPES`, `PHYSICAL_LOCATION`, `POINT_OF_CONTACT` | `DISCHARGES` (78%), `POPULATION_WASTEWATER` (77%), `EFFLUENT` (58%), `FLOW` (58%), `FACILITY_PERMIT` (55%), `AREAS_*` + `NEEDS_COST_BY_CATEGORY` + `REASON_FOR_NEEDS` (39% — federally-accepted needs only) | `POPULATION_DECENTRALIZED` (7%), `CET_INPUTS_NONPOINT` (7%), `CET_INPUTS_DECENTRALIZED` (7%), `UNIT_PROCESSES` (1%), `ASSET_MANAGEMENT` (0.03%) |

**Sample row** (CWNS_ID `48001257001`):
`FACILITIES.FACILITY_NAME = "Pettus WWTP"`, `FACILITIES.INFRASTRUCTURE_TYPE = "Wastewater"`,
`PHYSICAL_LOCATION.STATE_CODE = "TX"`, 15 nested tables populated.

**Anomalies / concerns**

- None. The CWNS load is the cleanest of the three Phase-1 federal
  loads so far: 100% identifier uniqueness, 100% within-state
  geography, identical schema hash across states.
- One side-finding: the iframe URL contains BOTH a `cs` and a
  `dialogCs` token — minted server-side when a state is selected. This
  is APEX's CSRF defense. Pure-HTTP forging of these tokens was
  attempted in the step-2 spike and confirmed impossible without
  driving the JS UI event. Playwright is the right tool.

**Deviations from the brief**

- None. The loader matches every spec point:
  - Path: `scrapers/federal/epa_cwns.py` ✓
  - Stable id: `CWNS_ID` → `source_record_id`, `source='epa_cwns_2022'` ✓
  - Payload shape: dict keyed by source table name; multi-row tables as
    lists ✓
  - Pre-upsert validation (ZIP magic, members, row counts, CWNS_ID
    uniqueness) ✓
  - Cross-state sanity check via PHYSICAL_LOCATION ✓
  - Operational metadata (scraper_run + source_signature per state) ✓
  - Sequential per state ✓
  - Headless committed default = True ✓

---

## 2026-05-11 — Day 2 step 2 (CWNS APEX download-state-zip spike — NEGATIVE)

**Spike outcome: FAIL.** The pure-HTTP 2-request anonymous flow can pull
the CWNS **Data Dictionary** (XLSX, 38 KB) but **cannot** pull state-scoped
CWNS data zips. Phase-1 CWNS loader (step 3) will use **Playwright**.
Time used: ~14 minutes of the 30-minute budget.

**What I tested**

1. GET `/ords/sfdw_pub/r/sfdw/cwns_pub/about` to capture the APEX session
   cookie (`ORA_WWV_APP_148`) and discover a download-state-zip link with
   embedded `session` and `cs` query parameters.
2. GET `/ords/sfdw_pub/r/sfdw/cwns_pub/download-state-zip` with
   `p2_location_id` set to each of: `TX`, `NC`, `48` (TX FIPS), `37`
   (NC FIPS), and `DD` (the value from the discovered link).
3. Re-fetched `/about` and `/data-download` to look for state-specific
   download URLs in the HTML.
4. Inspected the page's `<form>` action, hidden inputs, and inline
   `<script>` blocks.

**What I found**

- `p2_location_id=DD` returned a real ZIP. Inspecting it: it is the
  **"2022 CWNS Dashboard Data Dictionary"** (XLSX). 26 zip members; first
  shared-string is the page title; the workbook describes the 30+ CWNS
  tables (`FACILITIES`, `FACILITIES_CONFIRMED`, `FACILITY_TYPES`,
  `FACILITY_PERMIT`, `POINT_OF_CONTACT`, `PHYSICAL_LOCATION`,
  `AREAS_COUNTY`, etc.) and their fields. **Useful metadata; not state
  data.**
- `p2_location_id=TX|NC|48|37` all returned the **same** 5,112-byte HTML
  page titled "Download State ZIP" with no zip payload. The page is the
  APEX state-selector view, not an error page.
- The state-selector page contains an APEX form posting to
  `wwv_flow.accept?p_context=cwns_pub/data-download/<session>` with
  `p_flow_id=148`, `p_flow_step_id=5`, `p_instance=<session>`, and a
  CSRF token `p_page_submission_id`.
- The `<select id="P5_STATE">` element has 56 USPS-code options (AK..WY
  plus territories AS/GU/MP). Selecting a state in the UI is wired to
  `apex.widget.selectList("#P5_STATE", {"ajaxIdentifier": "<base64-ish
  token>"})`. The widget fires an AJAX call (likely to
  `/ords/sfdw_pub/wwv_flow.show`) when a state is picked. **The
  per-state `cs` and `dialogCs` tokens are minted server-side in
  response to that AJAX call** and only then is the per-state
  download URL valid.
- Only TWO non-DD pre-embedded state URLs appeared in the inline JS
  on `/data-download`: `p2_location_id=AK p3_type=State` (the
  default-first option in the dropdown) and two Native American
  tribal-area equivalents (`p3_type=NA_CSV`, `p3_type=NA_AC`). The
  other 55 jurisdictions' URLs are minted dynamically.

**Why the pure-HTTP flow fails**

The per-state `cs` (and `dialogCs`) tokens are not derivable client-side
— they are issued by the APEX server in response to the AJAX
state-change event. Without driving the JS event (selectList change → AJAX
request → server response → updated DOM link), no anonymous HTTP-only
flow can reach the state-scoped download endpoint. Forging
`p2_location_id` to a target state and reusing the DD-flow `cs` returns
the 5,112-byte selector HTML, not a zip.

**Forward plan for step 3 (Playwright fallback)**

The Playwright loader for CWNS should:

1. Launch Chromium (headless OK on free-tier CI; not required for local).
2. `page.goto("https://sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub/data-download")`.
3. Wait for the `#P5_STATE` select to be present (`page.wait_for_selector`).
4. `page.select_option("#P5_STATE", "TX")` to trigger the change event.
5. Wait for the page-update AJAX to settle. The download link's `href`
   should now have a fresh state-specific `cs` and `dialogCs`.
6. Intercept the download via `page.expect_download()` while clicking
   the download link, OR read the link's `href` and use the existing
   session cookies to fetch the zip via `requests` (cookies + session
   transferred from Playwright's context).
7. Validate the response is a real CWNS ZIP (ZIP magic + presence of
   `FACILITIES.csv` or equivalent member).
8. Persist the zip locally, parse the relevant CSV(s), upsert into
   `raw_facility_record` with `source='epa_cwns_2022'` and a stable
   per-row identifier (likely `CWNS_ID`).
9. Repeat for NC.
10. Write `scraper_run` + `source_signature` rows just as the ECHO
    loader does.

Playwright is already installed (`pip install playwright` and
`playwright install chromium` were run on Day 1). No new dependencies
needed.

**Useful inventory captured during the spike** (saved locally in
`local/cwns_spike/`, all gitignored):

- `about.html`            — the /about HTML
- `data_download.html`    — the /data-download HTML
- `download_default-DD_DD.zip` — the Data Dictionary XLSX (38 KB, 26 zip members)
- `download_USPS_TX.body`, `download_USPS_NC.body` — the 5,112-byte
  selector HTML responses for state codes
- `download_FIPS_48.body`, `download_FIPS_37.body` — same shape
- The `apex.widget.selectList` initializer for `#P5_STATE` with its
  `ajaxIdentifier` token (saved in `data_download.html`)

**Anomaly / non-issue**

- None. The spike worked exactly as designed — it set out to validate
  or disprove the 2-request flow and it disproved it cleanly.

---

## 2026-05-11 — Day 2 step 1 (EPA ECHO loader)

**Completed**
- Restructure commit `8cffbf2` landed cleanly: migrations moved from
  `db/migrations/` to `supabase/migrations/` with `<YYYYMMDDHHMMSS>_name.sql`
  naming. Single source of truth. Git rename detection at 100%; no byte
  changes on the SQL files themselves. `db/` tree removed. CI green on the
  new layout. Pooler sanity query via `.env` confirmed working
  (`SELECT COUNT(*) FROM source = 12`).
- **ECHO loader** at `scrapers/federal/epa_echo.py` — two-step REST flow
  (`get_facilities` → QID, `get_download` → CSV with qcolumns 1..34). Batch
  upsert into `raw_facility_record` with payload-hash-based no-change
  detection. Writes `scraper_run` + `source_signature` rows. Idempotent
  re-runs.
- Loaded TX and NC active CWA facilities. **92,326 raw rows inserted** total
  (72,499 TX + 19,827 NC — exact match to the Phase-0 audit predictions).
  - TX run: 81.4 s, `scraper_run.id=1`, `status=success`.
  - NC run: 26.6 s, `scraper_run.id=2`, `status=success`.
  - Schema hash identical across both states (`699dec10fc9a…`).
  - HTTP 200 on every setup + download call.
- Sample row (TX, POTW): `CITY OF PORT ARTHUR MAIN WWTP / TX0047589 / PORT
  ARTHUR / POTW`.

**Anomaly observed (informational, not a failure)**
- ECHO's `p_st` filter is **jurisdictional**, not geographic. Of 92,326
  rows, **11** (0.012%) have `CWPState ≠ 'TX' AND CWPState ≠ 'NC'`.
  Physical-state distribution: OK=3, VA=2, LA=2, SC=2, MD=1, AR=1 (= 11).
  Pull-origin attribution by pull-side arithmetic: TX pull contributed
  72,499 − 72,493 = **6** cross-state rows; NC pull contributed
  19,827 − 19,822 = **5** cross-state rows. Sum: 11. Reconciliation:
  72,493 TX + 19,822 NC + 11 cross = 92,326 ✓. Per-row pull-origin can be
  rederived later via `scraper_run_id` if needed; not required for
  Phase 3 handling.
- **Phase 3 filter rule (LOCKED):** the resolver in Phase 3 (Days 5-6)
  MUST filter raw rows where `raw_payload->>'CWPState' NOT IN ('TX', 'NC')`
  so cross-state rows never produce canonical entities. The filter is
  defined by the project's **state coverage set** — when a new state ships
  post-delivery, the filter widens via the same mechanism. Do not bypass
  the filter for cross-state rows when adding new states; instead, append
  the new USPS code to the coverage set. The rule applies to all sources
  whose query interfaces are jurisdictional rather than geographic (ECHO,
  any state-NPDES queries that join multi-state regions, and any future
  source with similar semantics). This entry is the canonical reference;
  Phase 3 design notes must restate the rule verbatim or link here.

**In progress**
- Step 2 of Phase 1 Day 2 — CWNS APEX `download-state-zip` spike (30-min
  time-box). Starts after this commit lands and CI confirms green.

**Blocked / pending**
- Ryan's go-ahead to start step 2 (after CI confirms green on this commit).
- Step 3 (CWNS loader) is gated on the outcome of step 2.

**Decisions made**
- ECHO column codes: `qcolumns=1..34` returns 34 useful columns including
  `RegistryID` (FRS), `SourceID` (NPDES), `CWPState`, `CWPZip`, `CWPCounty`,
  `FacLat`, `FacLong`, and `CWPFacilityTypeIndicator` — closing the audit's
  recorded gap. The default no-qcolumns response returns only 16 columns
  and is missing state/zip/county/FRS, so qcolumns is required.
- Source-record-id: prefer `SourceID` (NPDES), fall back to
  `FRS:<RegistryID>` when SourceID is blank. Never write a raw row without
  a stable identifier; skip-with-counter and continue.
- Idempotency: `ON CONFLICT (source_id, source_record_id) DO UPDATE … WHERE
  raw_facility_record.payload_hash <> EXCLUDED.payload_hash` so unchanged
  rows are a no-op write. `RETURNING (xmax = 0)` distinguishes inserts from
  updates. Unchanged rows are not in the RETURNING; we compute the
  unchanged count as `batch_size - inserted - updated`.
- Per-batch commits at `BATCH_SIZE=500`. ~145 commits for 72k TX rows;
  total commit overhead negligible vs. a single transaction's
  memory/pooler risk on free-tier.

**Deviations from the brief**
- None.

---

## 2026-05-11 — Day 1 (post-task-8 checkpoint: tasks 9–10, both checkpoint reviews approved)

**Completed**
- Checkpoint-2 self-review v1 and v2 — Ryan approved v2. Seven failures
  addressed; B2 CWNS APEX `download-state-zip` discovery captured.
- **Task 9** — wrote `.github/workflows/ci.yml` with three jobs: ruff lint
  + ruff format check, pytest, and schema-migration apply against a
  throwaway `postgres:16-alpine` service container. The schema job runs
  both migrations, verifies the source table has exactly 12 rows after
  seeding, asserts every expected slug is present, confirms
  `tceq_central_registry.robots_txt_status='disallow'`, and re-applies the
  seed to validate idempotency. All secrets surfaced (ANTHROPIC_API_KEY,
  BRAVE_API_KEY, SUPABASE_* family, ALERT_EMAIL) even though several are
  unset in GitHub Actions today.
- **Task 9** — wrote `.github/workflows/monthly_refresh.yml` stub on a
  `0 9 1 * *` cron (1st of each month, 09:00 UTC). Single placeholder step
  that prints a presence-only diagnostic for the secrets. Phase 5 replaces
  the body with the real load → resolve → enrich → export pipeline.
- Wrote a real pytest suite at `tests/test_geocoder.py`,
  `tests/test_facility_types_yaml.py`, and `tests/test_source_seed.py` so
  CI has 43 tests guarding the geocoder state-consistency policy
  (A1.6), the controlled vocabulary structure + regex compile (A3.x), and
  the source seed cardinality + locked decisions (A2.x). `tests/conftest.py`
  adds the project root to `sys.path` so imports work.
- Cleaned up `scrapers/sample_pulls.py` (removed 4 unused imports flagged
  by ruff). Ran `ruff format` across the project; 3 files reformatted.
- Local dry run before commit: `ruff check .` PASS, `ruff format --check .`
  PASS, `pytest -ra` 43/43 PASS.

**In progress**
- **Task 10** — staging files, committing as
  `Phase 1 Day 1: project skeleton, schema migration v0.1, source seed
  (12 rows), controlled vocab, geocoder stub, CI workflows. Checkpoint 2
  v2 approved.`, tagging `phase-1-day-1`, pushing to `main`. Will watch
  the CI run and report SHA + tag + CI status back.

**Blocked / pending**
- Ryan to approve the eventual Supabase migration apply as a separate
  authorized action after the push lands cleanly.
- Anthropic API key still PENDING_RYAN_PROVISION (Phase 4 blocker only).

**Decisions made**
- Ran `ruff format` across the repo before commit so CI's
  `ruff format --check .` step doesn't trip on opinionated formatting on
  the first push. Edits were mechanical (line-wrapping in
  `sample_pulls.py` and a few test files).
- Added `tests/conftest.py` to inject the project root onto `sys.path`.
  This is the minimal way to support `from orchestration.geocoder import
  ...` from test files without a `pyproject.toml`-level `pythonpath`
  edit. Future scrapers and loaders can import from any package via the
  same path.

**Deviations from the brief**
- None.

---

## 2026-05-11 — Day 1 (post-task-3 checkpoint: tasks 4–8)

**Completed**
- **Supabase CLI v2.98.2** installed to `%USERPROFILE%\bin` (not winget; no
  upstream winget package — direct GitHub release binary). `%USERPROFILE%\bin`
  added to user PATH.
- **`gh auth login`** completed via web flow, authenticated as
  `AxiomInsightsADMIN` (scopes: `gist, read:org, repo`; protocol HTTPS).
- **Task 4** — `git init -b main` in the project root; added `origin` remote
  to `https://github.com/AxiomInsightsADMIN/Arch-Legacy-Partners.git`;
  fetched and pointed local `main` at `origin/main` (commit `b1100ab`) using
  `git update-ref` + `git reset` so working-tree files were preserved.
  Remote `main` had stub `.gitignore` and `README.md`; my versions strictly
  supersede both. Added `.claude/` to `.gitignore`.
- **Task 5** — schema v0.1 written at `db/migrations/0001_initial.sql`. 14
  tables: `source`, `scraper_run`, `source_signature`, `raw_facility_record`,
  `canonical_facility`, `facility_record_link`, `field_provenance`,
  `canonical_facility_history`, `facility_type_lookup`, `geocoding_cache`,
  `llm_enrichment_cache`, `discovered_url`, `discovery_candidate_facility`,
  `discovery_review_queue`. UUID-keyed `canonical_facility`. Constraints
  enforce the locked architectural decisions (three-tier identity, separate
  provenance table, controlled-vocabulary acceptance flags, USPS state code,
  lat/long bounds). **Not yet applied** to Supabase.
- **Task 6** — `config/facility_types.yaml` written. Seven canonical types
  with synonyms, regex rules, deny-lists. YAML parses; all regex rules
  compile. **Not yet consumed** by any loader.
- **Task 7** — `db/migrations/0002_source_seed.sql` written. 11 source rows
  (the brief said "eight" but enumerated 11 — I included all enumerated).
  Idempotent via `ON CONFLICT (slug) DO UPDATE`. ToS posture and robots.txt
  status populated from Day-1 audit; the audit revealed that
  `www15.tceq.texas.gov/robots.txt` returns a full `Disallow: /`, so the
  `tceq_central_registry` row records `robots_txt_status='disallow'` and
  the notes route the loader to TCEQ Public Data Lookup instead. **Not yet
  applied** to Supabase.
- **Task 8** — recon script at `scrapers/sample_pulls.py` issued one
  read-only HTTP request per source. Findings:
  - **EPA ECHO** — two-step REST flow works (`get_facilities` → QID,
    `get_download` → CSV). 72,499 TX and 19,827 NC active CWA facilities.
    Sample CSVs trimmed to 100 rows each, saved to `local/source_samples/`
    (gitignored). Schema confirmed.
  - **EPA CWNS 2022** — entrypoint page describes downloadable CSV + Access
    DB via the 2022 CWNS Data Dashboard, which redirects to an Oracle APEX
    app at `sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub`. No direct CSV URL
    on the entrypoint; Phase 1 CWNS loader will need Playwright or APEX
    inspection.
  - **TCEQ CRPUB** — robots-disallowed; declined per locked decision 8.12.
  - **TCEQ Public Data Lookup** — 6 relevant lookup-path entries including
    waste management records and stormwater/wastewater status downloads.
  - **TCEQ Domestic Wastewater landing** — 14 relevant program links.
  - **NC DEQ DWR permits page** — 15 relevant program links (NPDES,
    non-discharge, industrial, municipal permitting).
  - **NC DEQ DWM solid waste landing** — 4 relevant; composting linked
    directly, transfer stations live deeper.
  - Audit doc at `docs/source_audit_phase0.md` with full per-source
    breakdown and two minor reclassifications to the section-9 mapping
    table (CWNS×POTW: Practical → Partial; ECHO×Land-Application: Partial
    confirmed).

**In progress**
- None. Stopping at the task-8 checkpoint per the brief.

**Blocked / pending**
- Ryan to review tasks 4–8 before any push to `main` (task 9 workflows
  + task 10 first commit).
- Schema and source-seed SQL not yet applied to Supabase — pending Ryan's
  review of the SQL itself.
- Anthropic API key still PENDING_RYAN_PROVISION (still a Phase-4 blocker,
  not a Phase-1/2 blocker).

**Decisions made**
- Supabase CLI installed via direct GitHub release rather than scoop or
  winget. Reasoning: winget has no Supabase package; installing scoop just
  to install supabase is excess scope. The Supabase binary on `%USERPROFILE%\bin`
  is a single ~98 MB file, easy to rotate when a new release ships.
- Section-9 ratings reclassified minimally: CWNS×POTW from Practical to
  Partial; ECHO×Land-Application confirmed at Partial. Documented in the
  audit doc.
- ECHO column codes pulled for the audit omitted state/zip/county codes
  (24/25/26). Recorded as a recon oversight in the audit doc, not a source
  gap — Phase 1 loader will include them.

**Deviations from the brief**
- Brief said "eight candidate sources" in task 7 but enumerated 11 — I
  seeded all 11. Logged for Ryan's review.
- Tasks 5 and 6 were drafted in parallel while `gh auth login` was pending
  Ryan's browser interaction. The git work in task 4 ran immediately after
  auth completed, before any of the SQL/YAML was committed (no commits
  yet; staging order doesn't matter at this stage). Surfacing for
  transparency.

---

## 2026-05-11 — Day 1 (scaffolding)

**Completed**
- Confirmed Python 3.11 install path. Installed Python 3.11.9 via winget (the
  machine had only Python 3.14 by default).
- Created Python 3.11 venv at `.venv` in the project root.
- Pinned and installed all base dependencies into `requirements.txt`
  (major-version pinned only, not frozen, per the brief).
- Installed Playwright Chromium (headless shell).
- Wrote project skeleton: directory tree, `.gitignore`, `README.md`,
  `pyproject.toml` (ruff + pytest configured), `LICENSE` (proprietary
  placeholder), `requirements.txt`, `.env.example`, and the local `.env` with
  the credentials from the brief.
- Confirmed `gh` (2.88.1) and `git` (2.53.0) CLIs available. Supabase CLI is
  not yet installed; deferred to task 4 (post-checkpoint).

**In progress**
- None. Stopping at the task-3 checkpoint per the brief.

**Blocked / pending**
- Ryan to review tasks 1-3 before any work past task 4 begins.
- Anthropic API key still PENDING_RYAN_PROVISION (not a blocker until Phase 4).
- Supabase CLI install pending (needed for `supabase init` / `supabase link`
  at task 4, after Ryan's checkpoint).
- Austin's GitHub username (deferred), CSV delivery preference, May-29
  delivery confirmation.

**Decisions made**
- Installed Python 3.11 via winget rather than pyenv-win or the python.org
  installer. Winget is the cleanest first-party path on Windows 11 and avoids
  shimming complexity for a single-Python-version project. If Ryan wants
  pyenv-win specifically (for multi-version dev work later), we can switch
  before any commits to the venv path land.
- Added `.env.example` alongside `.env`. The example file is committed so the
  secret schema is reviewable; the real `.env` is gitignored.

**Deviations from the brief**
- None.
