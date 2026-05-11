# Source Audit — Phase 0 (Day 1)

**Date:** 2026-05-11
**Author:** Axiom Insights
**Scope:** Read-only reconnaissance of each candidate data source, validating
or correcting the source-category mapping in section 9 of the kickoff brief.
**No database writes were performed.**

## How this audit was run

A Python recon script ([scrapers/sample_pulls.py](../scrapers/sample_pulls.py))
issued a single read-only HTTP request per source, saved the raw payload to
`local/source_samples/` (gitignored), and recorded:

- HTTP status
- bytes received
- rows or relevant link/anchor pairs observed
- a programmatic peek at the column headers (for tabular sources) or the
  document/PDF/data-download links present on landing pages (for HTML sources)

Sources whose target page did not expose direct CSV/XLSX hrefs were
probed further to locate the actual data endpoints (notably the EPA CWNS
Dashboard, which redirects from `cwnsdep.epa.gov/2022dashboard` to an Oracle
APEX application at `sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub`).

The robots.txt for every host was inspected before its first content fetch.
Results are recorded both here and on each `source` row in
`db/migrations/0002_source_seed.sql`.

## Headline findings

1. **TCEQ Central Registry (CRPUB) is robots-disallowed.** `www15.tceq.texas.gov/robots.txt`
   returns `User-agent: * / Disallow: /` — a full crawler ban. We will not
   scrape CRPUB. The same records are publicly republished as CSV/XLSX via
   the TCEQ Public Data Lookup at
   `https://www.tceq.texas.gov/agency/data/lookup-data`. The seed registry
   documents this in `notes` on the `tceq_central_registry` row.
2. **EPA ECHO works well via the documented REST API.** Two-step flow:
   `get_facilities` returns a `QueryID`; `get_download` returns rows for that
   QID. Sample pulls returned 72,499 active CWA facilities in Texas and 19,827
   in North Carolina. Schema includes NPDES ID, facility name, address line,
   city, lat/long, design/actual flow, and a POTW/NON-POTW indicator. ECHO's
   robots.txt explicitly disallows the search-results pages
   (`/facilities/facility-search/results/`) — we do not hit those; the bulk
   exporter is the supported path.
3. **EPA CWNS 2022 main-data downloads are not on a static href.** The
   public-facing page at
   `https://www.epa.gov/cwns/clean-watersheds-needs-survey-cwns-2022-report-and-data`
   advertises a "data dictionary and CWNS data as .csv files or as an Access
   Database" but the actual files are delivered through the 2022 CWNS Data
   Dashboard, which is an Oracle APEX application at
   `sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub`. Phase 1 CWNS loader will
   need either Playwright automation against the APEX app or a direct CSV
   export URL discovered by inspecting the APEX network traffic.
4. **The other five named sources are Practical or Partial as the
   section-9 mapping table predicted.** No corrections needed there.
5. **Operator-site, county-health, and state-registry placeholders remain
   placeholders.** Concrete sources for those rows will be enumerated during
   Phase 2 (Days 3-5).

## Per-source audit

### EPA ECHO — CWA REST API

| | |
|---|---|
| **Slug** | `epa_echo` |
| **Base URL** | `https://echodata.epa.gov/echo/cwa_rest_services.*` |
| **ToS posture** | Permissive (US gov, public data) |
| **robots.txt** | Allow root with `Crawl-delay: 10`. Disallows `/facilities/facility-search/results/` and `/detailed-facility-report/`. We use the REST API, not those pages. |
| **Sample size** | 100 rows (trimmed from 72,499 TX / 19,827 NC totals) |
| **Saved sample** | `local/source_samples/epa_echo_tx.csv`, `…_nc.csv` |

**Columns observed** (12, from `qcolumns=1,3,4,21,22,23,24,25,26,27,28`):

`CWPName, SourceID, CWPStreet, CWPCity, FacUsCanadaBorderFlg, CWPSICCodes,
CWPNAICSCodes, FacLat, FacLong, CWPTotalDesignFlowNmbr,
CWPActualAverageFlowNmbr, CWPFacilityTypeIndicator`

**Schema gap noticed in this pull:** my reconnaissance `qcolumns` set forgot
explicit FAC_STATE / FAC_ZIP / FAC_COUNTY codes. The state was implicit in the
filter (`p_st=TX`) and the zip/county are available via codes 24, 25, 26 (or
equivalent) — Phase 1 loader must request them. Not a source gap; a recon
oversight.

**Fields present vs. needed:**

| Required field | Present in ECHO? |
|---|---|
| name | yes (`CWPName`) |
| type | partial — `CWPFacilityTypeIndicator` is POTW/NON-POTW only |
| street | yes (`CWPStreet`) |
| city | yes (`CWPCity`) |
| zip | yes (column 25, not in this pull) |
| state | yes (column 24, implicit in filter) |
| county | yes (column 26, not in this pull) |
| latitude / longitude | yes (`FacLat` / `FacLong`) |
| accepts_septage | **no** — needs enrichment |
| accepts_grease_trap | **no** — needs enrichment |
| accepts_portable_toilet | **no** — needs enrichment |
| pricing | **no** — needs enrichment |
| phone / email / website | **no** — needs enrichment |
| FRS ID | yes (column 1) |
| NPDES ID | yes (`SourceID`) |
| state permit ID | **no** — for TX/NC these come from TCEQ / NC DEQ |

**Practical / Partial / Not Practical by category:**

| Category | Section 9 says | This audit says |
|---|---|---|
| POTW receiving stations | Practical | **Practical** — confirmed |
| County manhole programs | Not practical | **Not practical** — confirmed |
| Land application sites | Partial | **Partial** — some NPDES-permitted biosolids sites carry CWA permits, but most land application sites are state-only-permitted |
| Private/regional septage | Not practical | **Not practical** — confirmed |
| Composting | Not practical | **Not practical** — confirmed |
| Anaerobic digesters | Partial | **Partial** — confirmed; digesters with surface-water discharge appear in ECHO |
| Transfer stations | Not practical | **Not practical** — confirmed |

---

### EPA CWNS 2022

| | |
|---|---|
| **Slug** | `epa_cwns_2022` |
| **Entrypoint** | `https://www.epa.gov/cwns/clean-watersheds-needs-survey-cwns-2022-report-and-data` |
| **Data dashboard** | `https://cwnsdep.epa.gov/2022dashboard` → `https://sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub` (Oracle APEX) |
| **ToS posture** | Permissive (US gov) |
| **robots.txt** | `www.epa.gov` allow with admin-path Disallows; `cwnsdep.epa.gov` has no public robots.txt route (returns a login page). |
| **Sample size** | n/a (no static CSV/Access URL on the entrypoint; dashboard is interactive) |
| **Saved sample** | `local/source_samples/epa_cwns_2022.html` |

**What's actually downloadable from the static entrypoint:**

- Report to Congress PDF:
  `https://www.epa.gov/system/files/documents/2024-05/2022-cwns-report-to-congress.pdf`
- (No direct CSV / Access DB hrefs on the page.)

**Fields present vs. needed:** CWNS publishes by treatment-works ID with
facility name, location (CWNS-region rather than street), and treatment
categorization (primary, secondary, advanced, etc.) plus design flow. CWNS
does not publish hauler-acceptance flags, pricing, or contact details. It
is most useful as a corroborating list of POTWs (and detailed treatment-train
categorization) rather than as a primary entity inventory.

**Practical / Partial / Not Practical by category:**

| Category | Section 9 says | This audit says |
|---|---|---|
| POTW receiving stations | Practical | **Partial → reclassify** as Partial. CWNS lists POTWs but does not enumerate which accept hauled waste at a receiving station — that distinction is operational, not in CWNS. ECHO + state datasets are the actual primary sources for POTW receiving stations. |
| All other categories | Not practical | **Not practical** — confirmed |

**Phase 1 loader note:** CWNS loader needs either Playwright automation
against the Oracle APEX app or discovery of the underlying CSV export URL.
Recommend treating CWNS as a *secondary corroboration* source, not a
primary inventory source. This is a minor correction to section 9.

---

### TCEQ — Central Registry (CRPUB) — DO NOT SCRAPE

| | |
|---|---|
| **Slug** | `tceq_central_registry` |
| **Base URL** | `https://www15.tceq.texas.gov/crpub/` |
| **ToS posture** | Permissive in principle (public records) |
| **robots.txt** | **`User-agent: * / Disallow: /`** — total crawler ban |
| **Sample size** | n/a (declined, robots) |
| **Routing** | Use `tceq_public_data_lookup` instead |

Per locked decision 8.12, we honor the robots disallow. The same data is
public via the TCEQ Public Data Lookup downloads (see next section).

---

### TCEQ — Public Data Lookup (replacement for CRPUB)

| | |
|---|---|
| **Slug** | (rolled into the TCEQ source rows; landing page at this URL) |
| **Base URL** | `https://www.tceq.texas.gov/agency/data/lookup-data` |
| **ToS posture** | Permissive |
| **robots.txt** | `www.tceq.texas.gov` allow with `/search` and a few admin Disallows |
| **Sample size** | 6 relevant link/anchor pairs |
| **Saved sample** | `local/source_samples/tceq_public_data_lookup.html` |

**Relevant lookup paths found:**

- "Waste Management Permit Applications, Permits, Registrations, and Facilities"
  → `/agency/data/lookup-data/lookup-data/waste-mgmt-data-records.html`
  *(landfills, transfer stations, MSW, composting permits)*
- "Status of Stormwater and Wastewater Applications and Specifications"
  → `/agency/data/lookup-data/status-stormwater-wastewater.html`
  *(TPDES / domestic wastewater)*
- "Discharge Monitoring Report Data" → `/permitting/netdmr/netdmr#echo`
  *(corroborates ECHO)*
- "Status of Water-Supply Permits and Registrations"
  → `/agency/data/lookup-data/status-water-supply-permits.html`
  *(less relevant — drinking water side)*
- "Status of Air Permits and Permit Applications" *(not relevant for v1)*

**Phase 2 TCEQ loader plan:**

1. Pull the Waste Management Records download — covers TX transfer
   stations, MSW, composting registrations (categories 5, 6, 7).
2. Pull the Stormwater/Wastewater status download — covers TX POTWs and
   domestic wastewater permits (category 1).
3. Pull the Sludge/Biosolids dataset (linked from Domestic Wastewater
   landing) — covers TX land application sites (category 3).

---

### TCEQ — Domestic Wastewater landing

| | |
|---|---|
| **Slug** | `tceq_domestic_wastewater` |
| **URL** | `https://www.tceq.texas.gov/permitting/wastewater/municipal` |
| **ToS posture** | Permissive |
| **robots.txt** | Allow |
| **Sample size** | 14 relevant link/anchor pairs |
| **Saved sample** | `local/source_samples/tceq_domestic_wastewater.html` |

**Relevant program links observed:**

- "Municipal Domestic Wastewater Permits"
  → `/permitting/wastewater/municipal/WQ_Domestic_Wastewater_Permits.html`
- "Sewage Sludge and Biosolids: Permits for Land Application, Processing, or Disposal"
  → `/permitting/wastewater/sludge/WQ_sludge_ClassB_forms.html`
- "Wastewater Pretreatment" → `/permitting/wastewater/pretreatment/index.html`
- "Texas Pollutant Discharge Elimination System (TPDES)" landing
- "Wastewater Permit Applications Participating in the Review Process" *(useful for net-new discovery)*

**Note:** these are *program landing pages* — they describe processes and
forms, not facility lists. The actual permit lists / biosolids registries
are inside sub-pages or in the lookup-data downloads above. Phase 2 loader
crawls these sub-pages and pulls the linked PDF/XLSX permit registers.

**Practical / Partial / Not Practical:** As section 9 said —
**Practical** for POTW (1), land application (3), private septage (4), and
transfer stations (7) (via the related lookup-data feeds). **Partial** for
composting (5) and digesters (6) — TCEQ touches these but completeness is
state-dependent.

---

### NC DEQ — Division of Water Resources (DWR)

| | |
|---|---|
| **Slug** | `nc_deq_dwr` |
| **URL** | `https://www.deq.nc.gov/about/divisions/water-resources/water-resources-permits` |
| **ToS posture** | Permissive |
| **robots.txt** | Allow |
| **Sample size** | 15 relevant link/anchor pairs |
| **Saved sample** | `local/source_samples/nc_deq_dwr.html` |

**Relevant program branches observed:**

- "NPDES Wastewater" → `/about/divisions/water-resources/water-quality-permitting/npdes-wastewater`
- "Non-discharge Permitting" → `/about/divisions/water-resources/water-quality-permitting/non-discharge-branch`
- "Industrial Permitting"
- "Municipal Permitting" (via PERCS — Pretreatment, Emergency Response and Collection Systems)
- "Compliance and Expedited Permitting"

**Phase 2 NC DEQ DWR loader plan:** crawl NPDES Wastewater for POTWs; crawl
Non-discharge for biosolids residuals / land application permits. The
non-discharge branch is what covers NC's land application of biosolids and
septage (category 3) and is also where any state-level POTW receiving-station
information would live.

**Practical / Partial / Not Practical:** As section 9 said — **Practical**
for POTW (1) and land application (3). DWR doesn't cover transfer/composting
(those are DWM).

---

### NC DEQ — Division of Waste Management (DWM)

| | |
|---|---|
| **Slug** | `nc_deq_dwm` |
| **URL** | `https://www.deq.nc.gov/about/divisions/waste-management/solid-waste-section` |
| **ToS posture** | Permissive |
| **robots.txt** | Allow |
| **Sample size** | 4 relevant link/anchor pairs on the section landing |
| **Saved sample** | `local/source_samples/nc_deq_dwm.html` |

**Relevant program branches observed:**

- "Composting"
  → `/about/divisions/waste-management/solid-waste-section/composting`
- Three navigation links (Permitting Transformation Program, Express
  Permitting, Permit Assistance and Guidance)

**Schema gap noted:** the Solid Waste Section landing surfaces *Composting*
directly but does not surface *Transfer Stations* on the same page; transfer
stations live deeper under the Solid Waste Permitted Facilities list, which
NC DEQ publishes as a PDF facility roster updated periodically. Phase 2 NC
DEQ DWM loader will need to:

1. Crawl the Composting sub-page for active compost facility lists.
2. Find and crawl the Solid Waste Permitted Facilities roster (PDF or web
   listing) for transfer stations.

**Practical / Partial / Not Practical:** As section 9 said — **Practical**
for composting (5) and transfer stations (7).

---

### Placeholders (no concrete source pulled yet)

| Slug | Status |
|---|---|
| `state_npdes` | Placeholder for future expansion states (e.g. NY SPDES, CA SWRCB). For TX/NC v1, NPDES role is served by TCEQ Domestic Wastewater and NC DEQ DWR. No Day-1 pull. |
| `county_health_placeholder` | Top-N TX/NC counties to be enumerated during Phase 2 and attached as concrete source rows. |
| `state_registries_placeholder` | Concrete entries (e.g. TX Department of Agriculture, NC Department of Agriculture) to be added in Phase 2. |
| `operator_sites_placeholder` | Operator-specific source rows added per-operator in Phase 2 with per-operator ToS audits. |
| `discovery_crawl` | Driven in Phase 4.5 by Brave Search + Haiku extraction. Honors per-target-site robots.txt at fetch time. |

## Corrections to section 9 mapping table

Two minor reclassifications based on this audit:

1. **EPA CWNS 2022 × POTW receiving stations** — section 9 says "Practical";
   audit says **Partial**. CWNS does not distinguish receiving-station POTWs
   from non-receiving POTWs. Use ECHO + state data as primary; CWNS as
   corroboration.
2. **EPA ECHO × Land application** — section 9 says "Partial"; audit
   confirms Partial. Only land-application sites with surface-water
   discharge permits appear in ECHO. State sources (TCEQ Sludge/Biosolids,
   NC DEQ DWR Non-discharge) are the primary for category 3.

All other section-9 ratings stand as written.

## Checkpoint-2 follow-ups (2026-05-11 PM)

### B2 — CWNS APEX `download-state-zip` endpoint (DISCOVERY)

Checkpoint-2 verification fetched the CWNS dashboard at
`https://sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub` live. The page exposes
a session-scoped download endpoint:

```
/ords/sfdw_pub/r/sfdw/cwns_pub/download-state-zip
  ?p2_location_id=<STATE>
  &session=<APEX_SESSION_ID>
  &cs=<CSRF_TOKEN>
```

The `p2_location_id` parameter is APEX shorthand for the requested state.
`session` and `cs` are issued by the dashboard on first GET. Hypothesis: a
two-request anonymous flow (GET `/about` → capture `session`+`cs` →
GET `/download-state-zip?p2_location_id=TX&session=…&cs=…`) yields the
per-state CWNS data zip without driving the APEX UI.

**Phase-1 spike (30 min, executed at the federal data-load step):**

1. Issue the anonymous-session GET, extract `session` and `cs` from the
   landing page or the embedded APEX JS.
2. Replay the download endpoint with `p2_location_id=TX` and inspect the
   response: zip vs. HTML, content-type, byte size.
3. If a real zip arrives → unzip, sanity check the schema, document the
   flow, and wire the CWNS loader to it (no Playwright needed).
4. If not (HTML challenge, login redirect, expired token, etc.) → record
   the negative result in `docs/build_log.md`, and fall back to
   Playwright automation against the APEX app.

The `epa_cwns_2022` source row carries the same note so that future
maintainers don't lose this lead.

### Source count

This audit originally referenced eleven source rows in
`db/migrations/0002_source_seed.sql`. Following Checkpoint-2 decision
A2.3 + A2.5, **a twelfth row `tceq_public_data_lookup` was added** to
explicitly represent the TCEQ Public Data Lookup as a distinct source
(allow-listed via robots.txt), separate from `tceq_central_registry`
(disallow-listed) and `tceq_domestic_wastewater` (program landing).
Wherever this document says "11 sources" it should be read as 12.

## Phase-1 loader implementation order (recommended)

Tightest-to-loosest information density. Ordered by what the audit showed
about availability and schema completeness:

1. **EPA ECHO** — federal, large-N, two-step REST API, well-understood schema. Easiest first win.
2. **TCEQ Public Data Lookup** — TX coverage for categories 1, 3, 5, 7 in three datasets.
3. **NC DEQ DWR Non-discharge** — NC land-application + biosolids.
4. **NC DEQ DWM Composting** + **Solid Waste Permitted Facilities** — NC categories 5 and 7.
5. **EPA CWNS 2022** — secondary corroboration. Needs APEX/Playwright work; lower priority.

## Open items pending review

- **Schema migration v0.1** (`db/migrations/0001_initial.sql`) — not yet
  applied to Supabase. Awaits Ryan's review per task-3 checkpoint.
- **Controlled vocabulary** (`config/facility_types.yaml`) — not yet
  consumed by any loader. Awaits review.
- **Source registry seed** (`db/migrations/0002_source_seed.sql`) — not yet
  applied. Awaits review.

These three together complete tasks 5/6/7 of section 11 of the kickoff
brief. This audit doc completes task 8. After Ryan's review at the task-8
checkpoint, the build proceeds to tasks 9 (GitHub Actions workflows) and
10 (first commit + push to `main`).
