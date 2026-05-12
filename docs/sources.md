# Source Pull Documentation

One section per source slug seeded in the `source` table. Each section
follows the same shape for skimmability: name, slug, category, URL,
format, refresh cadence, robots.txt posture, ToS posture, fields
table, categories fed, gaps/quirks, audit cross-references.

Sixteen sections total, in this order:

1. [Federal — 2 sources](#federal-sources)
2. [Texas — 4 sources](#texas-state-sources)
3. [North Carolina — 5 sources](#north-carolina-state-sources)
4. [Placeholders — 4 sources](#placeholder-sources)
5. [Discovery — 1 source](#discovery-source)

Cross-references throughout:
- `docs/schema.md` — table-level field reference
- `docs/data_dictionary.md` — per-field semantics
- `docs/source_audit_phase0.md` — initial ToS / robots audit
- `docs/tceq_pdl_audit.md` — TCEQ Public Data Lookup reconnaissance
- `docs/nc_deq_audit.md` — NC DEQ discovery + edocs WAF findings
- `docs/v1_scope_limitations.md` — scope concessions and why

---

## Federal sources

### EPA Enforcement and Compliance History Online (ECHO)

**Slug:** `epa_echo`
**Category:** `federal`
**URL (program):** <https://echo.epa.gov/>
**URL (API):** `https://echodata.epa.gov/echo/cwa_rest_services.get_facilities` + `.get_download`
**Data format:** JSON setup → CSV download (34 columns, comma-delimited)
**Refresh cadence:** Live (REST API, on-demand). EPA publishes ECHO data daily; the v1 monthly cron pulls fresh per the locked schedule.
**robots.txt posture:** `allow` (with `Crawl-delay: 10`). `/facilities/facility-search/results/` and `/detailed-facility-report/` are disallowed — we use the bulk-data REST endpoint instead, which is explicitly allowed.
**ToS posture:** `permissive`. Public US-gov data. See <https://echo.epa.gov/resources/general-info/terms-of-service>.

**Fields available in source vs used in canonical**

| Source column | Used in canonical | Resolver field |
|---|---|---|
| `RegistryID` | yes | `frs_id` (cross-source ID-first key) |
| `SourceID` | yes | `npdes_id` (cross-source ID-first key) |
| `CWPName` | yes | `name` |
| `CWPStreet` | yes | `street` |
| `CWPCity` | yes | `city` |
| `CWPState` | yes | `state` (filtered to `{TX, NC}`) |
| `CWPZip` | yes | `zip` |
| `CWPCounty` | yes | `county` |
| `CWPLatitude` / `CWPLongitude` | yes | `latitude` / `longitude` (zero-zero → NULL) |
| `CWPSICCodes` | yes | `raw_facility_type_string` for YAML lookup |
| 24 other columns (flow, permit dates, enforcement status, etc.) | no | preserved in `raw_payload`; available for Phase 4 enrichment |

**Categories fed:** Primarily seeds the cross-state spine of canonical_facility entities. Most ECHO rows are industrial NPDES dischargers that fall **outside** the 7-category v1 scope (~70K rows with `facility_type=NULL`). The SIC-4952 subset (~5K rows) is the Phase-4 promotion candidate for `potw_receiving_station` when an acceptance signal is found.

**Known gaps / quirks**
- **CWPState stragglers**: 11 rows have `CWPState` outside `{TX, NC}` (OK 3, VA 2, LA 2, SC 2, MD 1, AR 1). Filtered by `resolver/_filters.py` at canonical-resolution time per the pinned filter rule.
- **Coordinates as 0/0**: ECHO uses (0.0, 0.0) as a "no geocoder result" sentinel. The resolver normalizes that to NULL coords.
- **`SourceID` vs `RegistryID` per row**: Some rows have one but not both. Resolver's source-record-id fallback is `SourceID` → `FRS:<RegistryID>`.

**Audit notes**
ECHO was the Phase 1 Day 2 step 1 loader. Two scraper runs (one per state) were original behavior; consolidated into one signature per logical refresh post-Phase-5-follow-on (commit `9a6eb53`). Within-ECHO FRS-based collapse contributed ~11,131 ID-first merges in the current resolver state. Cross-source NPDES merges with CWNS: 89 (CWNS NPDES coverage is sparse). See `docs/source_audit_phase0.md` for the initial audit and `docs/build_log.md` → "Day 2 step 1" for the loader entry.

---

### EPA Clean Watersheds Needs Survey 2022 (CWNS)

**Slug:** `epa_cwns_2022`
**Category:** `federal`
**URL (program):** <https://www.epa.gov/cwns>
**URL (download):** `https://sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub/data-download` (Oracle APEX app — JS-driven state-picker + modal)
**Data format:** Per-state ZIP of CSV tables keyed by `CWNS_ID` (33 member CSVs; we ingest 25).
**Refresh cadence:** The 2022 release is the current dataset. CWNS publishes new full releases on an irregular ~4-year cadence (2008 → 2012 → 2022). The monthly cron will pull the same 2022 dataset until EPA publishes a successor.
**robots.txt posture:** `allow` for sdwis.epa.gov.
**ToS posture:** `permissive`. Public US-gov data.

**Fields available in source vs used in canonical**

CWNS raw_payload is a nested JSONB keyed by source-CSV table name. Used:

| Source path | Used in canonical | Resolver field |
|---|---|---|
| `PHYSICAL_LOCATION.CWNS_ID` | yes | `source_record_id` + `cwns_id` (FIPS-prefixed; first two digits encode state) |
| `PHYSICAL_LOCATION.STATE_CODE` | yes | `state` |
| `PHYSICAL_LOCATION.CITY` | yes | `city` |
| `PHYSICAL_LOCATION.ADDRESS` | yes | `street` |
| `PHYSICAL_LOCATION.ZIP_CODE` | yes | `zip` |
| `PHYSICAL_LOCATION.COUNTY_NAME` | yes | `county` |
| `PHYSICAL_LOCATION.LATITUDE` / `LONGITUDE` | yes | `latitude` / `longitude` |
| `FACILITIES[0].FACILITY_NAME` | yes | `name` |
| `FACILITY_PERMIT[*]` where `PERMIT_SOURCE='NPDES'` | yes (sparse — ~100 of 3,132 rows) | `npdes_id` (cross-source ID-first key) |
| `FACILITY_TYPES[*].FACILITY_TYPE` | yes | `raw_facility_type_string` for YAML lookup |
| 22 other sub-tables (DISCHARGES, EFFLUENT, FLOW, POPULATION_*, CET_INPUTS_*, etc.) | no | preserved in `raw_payload`; available for Phase 4 |

**Categories fed:** Spine for `potw_receiving_station` — most CWNS rows are public WWTPs and collection systems, but per the A3.3 receiving-station-specific framing (see `data_dictionary.md` §3) we only promote when explicit acceptance signal found. Today: 124 NC-side and 0 TX-side promotions via NC ND PERMIT_TYPE; Phase 4 expands.

**Known gaps / quirks**
- **Sparse NPDES**: only ~100 of 3,132 rows carry `PERMIT_SOURCE='NPDES'` in `FACILITY_PERMIT`. Cross-source overlap with ECHO via NPDES is 89 rows. CWNS treats permit data as side-information, not primary attribute.
- **Playwright required**: The data-download flow requires JS-driven interaction with the APEX `<select id="P5_STATE">` and a follow-on `theme42.dialog` modal. Pure HTTP can only retrieve the nationwide Data Dictionary (verified in Day 2 step 2 spike).
- **Per-state ZIP** — federal loader iterates states; consolidated signature written once at the end of the run (Phase 5 follow-on `9a6eb53`).

**Audit notes**
Phase 1 Day 2 step 3 loader. The Day 2 step 2 spike confirmed Playwright is required (the session-scoped `/download-state-zip?...&cs=...` URL needs a captured session token; pure HTTP can't get it). Sequential per-state because APEX session state collides on parallel selects. See `docs/build_log.md` → "Day 2 step 3" for the loader entry and "Day 2 step 2" for the negative HTTP-only spike.

---

## Texas state sources

### TCEQ Central Registry (CRPUB)

**Slug:** `tceq_central_registry`
**Category:** `state`
**Status:** **Declined per locked decision 8.12** — not scraped.

**URL:** <https://www15.tceq.texas.gov/crpub/>
**Data format:** HTML search results + per-record detail pages.
**Refresh cadence:** N/A — not scraped.
**robots.txt posture:** `disallow`. `www15.tceq.texas.gov/robots.txt` is `User-agent: * / Disallow: /` (total crawler ban).
**ToS posture:** `permissive` (data is public; TCEQ does not prohibit access, only crawling of this subdomain).

**Why declined**

Locked decision 8.12 requires per-source ToS audits and bars us from scraping robots-disallowed paths. CRPUB carries the most detailed TCEQ regulatory records (RN-keyed cross-program data), but we cannot crawl this subdomain. Records are obtained via the umbrella `tceq_public_data_lookup` (slug below), which republishes CRPUB datasets as structured CSV/XLSX downloads on an allowed path.

**Audit notes**
See `docs/v1_scope_limitations.md` §1 for the locked-decision walkthrough and `docs/tceq_pdl_audit.md` for the reconnaissance that established the Public Data Lookup as the supported alternative.

---

### TCEQ Public Data Lookup

**Slug:** `tceq_public_data_lookup`
**Category:** `state` (umbrella)
**URL:** <https://www.tceq.texas.gov/agency/data/lookup-data>
**Data format:** HTML index page linking to per-program downloads (XLS / XLSX / CSV).
**Refresh cadence:** Varies by sub-program; the MSW XLS (the only currently-wired child) refreshes weekly.
**robots.txt posture:** `allow` for `www.tceq.texas.gov`. Only `/search` and a few admin paths are disallowed.
**ToS posture:** `permissive`. Website Policies index: <https://www.tceq.texas.gov/help/policies/index.html>. The Public Domain and Linking Policy states TCEQ web content is public domain unless otherwise noted.

**Role**

Umbrella row for TCEQ data-download surfaces. The actual loaders attach to children of this row (e.g. `tceq_msw_facilities_xls`). Six relevant lookup paths surfaced in Day 1 reconnaissance; only one (MSW) is wired in v1. Adding a new TCEQ sub-source means: append a migration with the child slug, wire a loader, point the loader's `get_source_id(cur, '<slug>')` call at the new slug.

**Audit notes**
See `docs/tceq_pdl_audit.md` for the 3-round reconnaissance (13 → 5 → 3 candidate sub-pages) that mapped TCEQ's data-download universe.

---

### TCEQ MSW Active Facilities (msw-facilities-texas.xls)

**Slug:** `tceq_msw_facilities_xls`
**Category:** `state` (active loader; child of `tceq_public_data_lookup`)
**URL:** `https://www.tceq.texas.gov/downloads/permitting/waste/msw/active-facilities/msw-facilities-texas.xls` (static URL, weekly-refreshed)
**Data format:** Legacy BIFF .xls (824 KB). Parsed with the `xlrd` engine (NOT openpyxl — wrong format).
**Refresh cadence:** Weekly. TCEQ publishes a fresh file most Fridays.
**robots.txt posture:** `allow` (file lives at `www.tceq.texas.gov/downloads/...`, an allowed path).
**ToS posture:** `permissive` (TCEQ Public Domain and Linking Policy).

**Fields available in source vs used in canonical**

22 columns in source; 12 used:

| Source column | Used in canonical | Resolver field |
|---|---|---|
| `RN` | yes | `tceq_rn` (within-TCEQ entity-level ID-first key) |
| `Additional ID` | yes | `tceq_additional_id` + `source_record_id` + `state_permit_id` |
| `Site Name` | yes | `name` |
| `Physical Type` | yes | `raw_facility_type_string` for category map |
| `Physical Site Status` | yes (filter) | Resolver drops rows where this is `'NOT CONSTRUCTED'` |
| `Phys Addr Line 1` | yes | `street` |
| `Phys Addr City` | yes | `city` |
| `Phys Addr State` | yes | `state` |
| `Phys Addr Zip` | yes | `zip` |
| `County` | yes | `county` |
| `Latitude` / `Longitude` | yes | `latitude` / `longitude` |
| 10 other columns (Region, Program, Legal Status, Near Phys Loc *, etc.) | no | preserved in `raw_payload` |

**Categories fed**

Per GI-613 (the official TCEQ Physical Type → category mapping, captured at `docs/build_log.md` → "TCEQ Physical Type → v1 canonical-category mapping"):

- `composting_facility` — `Physical Type ∈ {5RC, 5RCX}` (~172 rows; 5RCX is NOI-tier, medium confidence)
- `anaerobic_digester` — `Physical Type='9GR'` (~45 rows; the only v1 cat-6 contribution)
- `transfer_station` — `Physical Type ∈ {5TS, 5LV, 5CC}` (~388 rows; 5CC = Citizens Collection Stations per GI-613 small-subtype designation)
- `private_regional_septage_facility` — `Physical Type ∈ {5GG, 5TL, 5GM}` (~80 rows max; liquid-waste subset, partial)

**Known gaps / quirks**
- **256 rows have `Physical Site Status='NOT CONSTRUCTED'`** — permitted-but-never-built facilities, no physical site to canonicalize against. Filtered by `resolver/_filters.py` at canonical resolution.
- **Legacy BIFF .xls** requires the `xlrd` engine; modern XLSX uses openpyxl. Both pinned in `requirements.txt`.
- **No TPDES** (Texas-equivalent of NPDES) included in this file. Domestic wastewater permits live separately under `tceq_domestic_wastewater` (not yet wired).
- **GI-613 mapping is range-coded by Auth Number** — see Day-1 audit for the full table.

**Audit notes**
Phase 2 step 1 loader (commit thread starting `3e5f2eb`). GI-613 fetched and decoded at Day-1 audit; captured at `docs/build_log.md` → "Phase 2 housekeeping (CI iterative migrations + GI-613 capture)". The schema migration `20260511221000_add_last_modified_to_source_signature.sql` was prompted by this loader needing to capture the HTTP Last-Modified header.

---

### TCEQ Domestic Wastewater Permits

**Slug:** `tceq_domestic_wastewater`
**Category:** `state`
**Status:** Not yet wired. Source row exists for future expansion.

**URL:** <https://www.tceq.texas.gov/permitting/wastewater/municipal>
**Data format:** Mix of HTML program pages, PDF process documents, and per-page indices. Facility-list downloads would route through `tceq_public_data_lookup` once wired.
**Refresh cadence:** Unknown until wired; TCEQ updates municipal-permit pages irregularly.
**robots.txt posture:** `allow`.
**ToS posture:** `permissive`.

**Why not wired in v1**
Phase 1 audit focused on the MSW XLS as the dominant TCEQ data source. The TPDES list is reachable via Public Data Lookup but the v1 brief doesn't gate on it (cat-1 POTWs are covered by CWNS + ECHO). A future-state wire-up would expand TX cat-1 coverage if Phase 4 acceptance-flag enrichment proves insufficient.

---

## North Carolina state sources

### NC DEQ Division of Water Resources (DWR)

**Slug:** `nc_deq_dwr`
**Category:** `state` (umbrella)
**URL:** <https://www.deq.nc.gov/about/divisions/water-resources>
**Role:** Umbrella row for NC DWR sub-sources. The active loader (`nc_deq_non_discharge_facilities`) hangs off this slug semantically; for FK organization it has its own row.
**robots.txt posture:** `allow`.
**ToS posture:** `permissive`.

---

### NC DEQ DWR Non-Discharge Facilities

**Slug:** `nc_deq_non_discharge_facilities`
**Category:** `state` (active loader; child of `nc_deq_dwr`)
**URL (discovery):** NC DEQ DWR Locator Map Experience (an AGOL Experience Builder app linked from the DWR program pages)
**URL (data):** ArcGIS FeatureServer at `https://services2.arcgis.com/kCu40SDxsCGcuUWO/arcgis/rest/services/NPDES_Non_Discharge_Permits_(View)/FeatureServer/0`
**Data format:** ArcGIS REST JSON (paginated via `resultOffset` / `resultRecordCount`).
**Refresh cadence:** Live (FeatureServer queried on demand). NC DEQ updates the underlying layer continuously as permits change.
**robots.txt posture:** `allow` on `services2.arcgis.com` (Esri's AGOL hosting).
**ToS posture:** `permissive`. NC DEQ policies: <https://www.deq.nc.gov/about/policies>.

**Fields available in source vs used in canonical**

16 ArcGIS attribute columns; 7 used:

| Source attribute | Used in canonical | Resolver field |
|---|---|---|
| `PERMITNUMBER` | yes | `source_record_id` + `nc_permit_number` (`WQ\d{7}` format) + `state_permit_id` |
| `FACILITY` | yes | `name` (fallback: `OWNER`) |
| `OWNER` | partial | fallback for `name` |
| `COUNTY` | yes | `county` |
| `PERMIT_TYPE` | yes | `raw_facility_type_string` for category map (substring rules) |
| `URL` | yes | `website` (links to NC eDocs detail page) |
| `ObjectId` | no (volatile) | not stored as ID |
| 9 other attributes (PERMIT_STATUS, FACILITY_STATUS, MAJOR, REGION, dates, etc.) | no | preserved in `raw_payload` |

**Categories fed**

NC ND PERMIT_TYPE substring matching in `resolver/_category_map.py`:
- `'residual solids'` → `land_application_site` (158 rows: 503 + 503-Exempt land app / distribution / surface disposal)
- `'reclaimed water'` → `potw_receiving_station` (124 rows)
- `'wastewater irrigation'` → `private_regional_septage_facility` (borderline; 589 SFR rows — see Phase 4 SFR filter pin)
- `'closed-loop recycle'` → NULL (industrial; out of scope)
- `'high rate infiltration'` → NULL (out of scope)

**Known gaps / quirks**
- **Geometry stripped**: 0 of 1,259 features carry geometry. NC DEQ deliberately suppressed coordinates for privacy because 47% of rows are single-family residences. County-attribution is the geographic anchor. See `docs/v1_scope_limitations.md` for the locked workaround.
- **No street column**: `FACILITY` sometimes contains an address-like SFR name (e.g. "972 New Elam Church Rd. SFR") but it's not a reliable address field. The Phase 5 geocoder backfill tried `<FACILITY>, <COUNTY>, NC` and got 7% success.
- **SFR over-merge risk**: NC ND residential permits share name strings with NC SF septage businesses at the same address. Phase 4 design pin (`build_log.md` → "Phase 4 design notes: residential-address-pattern filter for resolver") addresses this at the resolver layer.

**Audit notes**
Phase 2 step 2 loader. NC OneMap discovery was a dead-end (documented at `docs/nc_deq_audit.md` so future maintainers don't re-walk); the live data lives in the AGOL org `ncdenr.maps.arcgis.com` reached via the DWR Locator Map Experience item. ArcGIS FeatureServer discovery chain pinned at `docs/build_log.md` → "Phase 2 step 2 follow-up pins".

---

### NC DEQ Division of Waste Management (DWM)

**Slug:** `nc_deq_dwm`
**Category:** `state` (umbrella)
**URL:** <https://www.deq.nc.gov/about/divisions/waste-management>
**Role:** Umbrella row for NC DWM sub-sources. Two active loaders hang off this slug semantically: `nc_deq_solid_waste_facility_list` and `nc_deq_septage_firm_list`.
**robots.txt posture:** `allow` on `www.deq.nc.gov`.
**ToS posture:** `permissive`.

---

### NC DEQ DWM Solid Waste Permitted Facilities

**Slug:** `nc_deq_solid_waste_facility_list`
**Category:** `state` (active loader; child of `nc_deq_dwm`)
**URL (program):** <https://www.deq.nc.gov/about/divisions/waste-management/solid-waste>
**URL (data, attempted):** `https://edocs.deq.nc.gov/WasteManagement/ElectronicFile.aspx?docid=2132701&dbid=0&repo=WasteManagement`
**URL (data, actual):** local manual-drop at `local/manual_drops/nc_deq_solid_waste/*.xlsx`
**Data format:** XLSX (modern Office Open XML). Two sheets — "About" (40-row metadata; carries content-date) and "Active Solid Waste Facilities" (435 facility rows × 13 columns).
**Refresh cadence:** NC DEQ DWM publishes monthly. v1 captures one snapshot per manual-drop cycle.
**robots.txt posture:** `unknown` for `edocs.deq.nc.gov` — the page never loads cleanly via Playwright so robots.txt isn't reached.
**ToS posture:** `permissive` (NC DEQ policies).

**Manual-drop fallback (WHY)**

`edocs.deq.nc.gov` enforces a **network-layer WAF rule** that blocks Playwright (real Chromium, headless or headed) with `net::ERR_CONNECTION_TIMED_OUT` and `net::ERR_NETWORK_CHANGED`. Pure HTTP `requests.get` hits the same block. Per locked operational rule, NO anti-detection escalation is attempted. Workflow:

1. Operator (on a workstation with browser-state cookies) downloads the XLSX from edocs.
2. Drops the file into `local/manual_drops/nc_deq_solid_waste/`.
3. Loader's `fetch_source()` tries Playwright first (will fail), then falls back to `_newest_manual_drop()` which picks up the newest XLSX.

**Fields available in source vs used in canonical**

13 columns in source; 11 used:

| Source column | Used in canonical | Resolver field |
|---|---|---|
| `Facility Id` | yes | `source_record_id` + `nc_facility_id` (composite `<county>-<type>-<year>` format) + `state_permit_id` |
| `Facility Name` | yes | `name` |
| `Activity` | yes | `raw_facility_type_string` for category map |
| `Address` | yes | `street` |
| `City` | yes | `city` |
| `State` | yes | `state` |
| `Zip` | yes | `zip` |
| `County` | yes | `county` |
| `Latitude` / `Longitude` | yes (97.7% populated) | `latitude` / `longitude` |
| `Phone` | yes | `phone` |
| `Contact` | no | preserved in `raw_payload` |
| `Waste` | no (informational) | preserved (decoder: MSW, LCID, CD, Type I, HHW, etc.) |

**Categories fed**

NC SW Activity overrides in `resolver/_category_map.py`:
- `Trans` (92 rows) → `transfer_station`
- `Compost` (63 rows) → `composting_facility`
- `Collection` (30 rows) → `transfer_station` per source code, BUT **filtered out** by HHW Collection rule unless explicit hauler-receiving capability is documented (none observed currently)
- `LF` (175), `LF*` (15), `TP` (52), `MatRecovery` (7), `Incin` (1) → NULL (out of 7-cat scope)

**Known gaps / quirks**
- **Playwright always fails**: WAF rule at network layer. Manual-drop is the operational primary.
- **Content date in About sheet**: "Date Created: April 28, 2026" parsed via regex into RFC 7231 for `source_signature.last_modified`.
- **97.7% lat/lng coverage** (425 of 435 rows). The remaining 10 are gap-known.
- **30 HHW Collection rows excluded** from `transfer_station` by default per pinned filter rule.

**Audit notes**
Phase 2 step 3 loader (commit `986f62a`, format-fixed in `befb5b0`). File format originally assumed PDF in audit; verified XLSX by Ryan from File Properties. See `docs/build_log.md` → "Phase 2 step 3" and `docs/v1_scope_limitations.md` for the WAF-block scope concession.

---

### NC DEQ DWM Septage Firm Registry

**Slug:** `nc_deq_septage_firm_list`
**Category:** `state` (active loader; child of `nc_deq_dwm`)
**URL (program):** <https://www.deq.nc.gov/about/divisions/waste-management/solid-waste/septage-program>
**URL (data, attempted):** `https://edocs.deq.nc.gov/WasteManagement/ElectronicFile.aspx?docid=2132702&dbid=0&repo=WasteManagement`
**URL (data, actual):** local manual-drop at `local/manual_drops/nc_deq_septage_firm/*.xlsx`
**Data format:** XLSX with one data sheet named `PermittedSeptageForm_<YYYYMMDD>` (e.g. `PermittedSeptageForm_20260428`).
**Refresh cadence:** NC DEQ DWM publishes monthly; date is encoded in the sheet name.
**robots.txt posture:** `unknown` (same edocs WAF block).
**ToS posture:** `permissive`.

**Manual-drop fallback:** Same mechanism as `nc_deq_solid_waste_facility_list`. Both files come from the same edocs document repository (`docid=2132702` vs `docid=2132701`).

**Fields available in source vs used in canonical**

9 columns in source; 7 used:

| Source column | Used in canonical | Resolver field |
|---|---|---|
| `Permit` | yes | `source_record_id` + `nc_septage_permit` (`NCS-\d{5}`) + `state_permit_id` |
| `Name` | yes | `name` |
| `Address` | yes | parsed: `street; city` semicolon-delimited → `street` + `city` |
| `County` | yes | `county` (NULL when `'-'` for out-of-state firms) |
| `Phone` | yes | `phone` |
| `Activity` | yes (uniform `'Hauler'`) | `raw_facility_type_string` → `private_regional_septage_facility` via source override |
| `Waste` | no (uniform `'Septage'`) | preserved |
| `Status` | no (uniform `'Open'`) | preserved |
| `Contact` | no | preserved |

**Categories fed**

100% `private_regional_septage_facility`. Every row is a regulated septage hauler firm (Activity uniformly `'Hauler'`); pure cat-4 source. No per-row classifier decoding needed at the resolver — `_category_map.NC_SF_ACTIVITY_OVERRIDES` maps the constant.

**Known gaps / quirks**
- **No state column**: state is `'NC'` by source-of-record geography, except for `County='-'` rows.
- **1 out-of-state firm**: `NCS-01837 Blue Diamond Portable Restrooms`, address `115 Juniper Ridge Road; Conway`. Conway has no NC ZIP and is the SC seat of Horry County — a SC-physical firm holding a NC-DEQ Septage Hauler permit to operate inside NC. Kept in load; flagged for Phase 3 review as `NC-jurisdiction-with-SC-physical-site`.
- **No native coords**: source has no Latitude/Longitude. Phase 5 geocoder backfill geocoded 71% of rows via `<street>, <city>, NC` synthesis (525 high + 11 low-confidence state-mismatch).
- **Content date from sheet name**: regex `r"(\d{8})$"` against `PermittedSeptageForm_20260428` → RFC 7231 `Tue, 28 Apr 2026 00:00:00 GMT`.

**Audit notes**
Phase 2 step 4 loader (commit `986f62a`). See `docs/build_log.md` → "Phase 2 step 4". WAF-block scope concession documented at `docs/v1_scope_limitations.md`.

---

## Placeholder sources

These four rows exist in `source` so that future loaders or per-county / per-operator entries can attach without a fresh migration each time. No scraper currently exists; no rows have ever flowed through these slugs.

### `county_health_placeholder`

**Category:** `county` (placeholder)
**URL:** N/A
**Refresh cadence:** N/A
**Activation path post-handoff:** Per locked decision, per-county health-department sources get concrete slugs of the form `county_health_<state>_<county>` (e.g. `county_health_tx_harris`). A new migration appends the row; a new loader at `scrapers/county/<state>_<county>.py` pulls and upserts. Top-N counties are an explicit Phase 2 deliverable (deferred to Phase 6 if not surfaced earlier).

Documentation cross-ref: `docs/runbook_add_a_state.md` (handles the parallel "add a new state" case) and `docs/source_audit_phase0.md` for the audit template.

### `state_npdes`

**Category:** `registry` (placeholder)
**URL:** N/A
**Refresh cadence:** N/A
**Activation path post-handoff:** Placeholder for future expansion-state NPDES interfaces (NY SPDES, CA SWRCB, etc.). For v1, NPDES coverage in TX is served by TCEQ's TPDES (not currently wired; see `tceq_domestic_wastewater`); NC NPDES is served by NC DEQ DWR (`nc_deq_non_discharge_facilities` covers the non-discharge slice; surface-water NPDES is via ECHO cross-reference).

### `state_registries_placeholder`

**Category:** `registry` (placeholder)
**URL:** N/A
**Refresh cadence:** N/A
**Activation path post-handoff:** Placeholder for state-specific registries that fall outside DEQ/TCEQ (e.g. state Department of Agriculture biosolids registries). Concrete entries land via a per-source migration with a specific slug (e.g. `tx_agriculture_biosolids`).

### `operator_sites_placeholder`

**Category:** `operator_site` (placeholder)
**URL:** N/A
**Refresh cadence:** N/A
**Activation path post-handoff:** Placeholder for individual operator websites (Synagro, Denali, regional haulers). Each concrete operator site gets its own source row with its own ToS audit (default-restrictive until proven otherwise). The expected workflow is to discover candidates via the discovery crawl (`discovery_crawl` source below), confirm via human review, then promote to a concrete operator-site source row when the candidate passes review.

---

## Discovery source

### Discovery Crawl (Brave Search + Haiku extraction)

**Slug:** `discovery_crawl`
**Category:** `discovery_crawl`
**URL:** N/A — internal source category.
**Data format:** Per-URL fetched content (HTML, PDF, JSON) plus Haiku structured-extraction output stored at `discovery_candidate_facility.raw_payload`.
**Refresh cadence:** On-demand; bounded by per-(category × state) budget caps in YAML config. The monthly cron does NOT run discovery automatically — Phase 4.5 architectural decision keeps discovery operator-triggered.
**robots.txt posture:** `none` at the source-row level (no single URL); per-target robots.txt honored at fetch time by the crawler.
**ToS posture:** `permissive` at the orchestration level; per-target ToS audit happens when a candidate URL is fetched.

**Architecture (Phase 4.5 build target)**

The pipeline reads from four schema tables:

1. **`discovered_url`** — Brave Search produces candidate URLs per (category × state) query bucket. Bounded by YAML budget caps. Each URL gets a content-hash on fetch and a classified-relevance verdict (`relevant` | `unrelated` | `uncertain`).
2. **`discovery_candidate_facility`** — Haiku extracts structured facility candidates from the fetched content, writing structured JSON into `raw_payload` with a `classification_confidence` tier.
3. **`discovery_review_queue`** — Locked decision: **discovery cannot auto-create canonical_facility rows**. Every Haiku-extracted candidate that looks net-new sits in this queue with a `hold_reason` until a human resolves it (`approved_new` | `merged_existing` | `rejected`).
4. **`canonical_facility`** — only receives discovery-extracted candidates that pass human review with `resolution='approved_new'`. The link comes via `facility_record_link.match_method='discovery_extract'`.

**Categories fed (target)**

All seven v1 categories, but with emphasis on the v1 gaps:
- `county_manhole_program` (0 rows from v1 sources; discovery is the only path)
- `potw_receiving_station` (Phase 4 acceptance-flag enrichment overlap)
- TX biosolids land application (v1 gap on TX; CRPUB declined)
- NC anaerobic digesters (v1 gap on NC)

**Known gaps / quirks**
- **Brave API key required** (`BRAVE_API_KEY` secret in GitHub Actions / `.env`)
- **Anthropic API key required** (`ANTHROPIC_API_KEY` for Haiku extraction)
- **Not yet built**: Phase 4.5 is post-Phase-4 enrichment. Discovery pipeline code lives in `enrichment/` once it ships.
- **Per-target robots.txt honored at fetch time**: each candidate URL is checked against the target site's robots.txt before fetch; disallowed URLs get `fetch_status='skipped'`.

**Audit notes**
The discovery architecture is locked in the kickoff brief (section 8.11 and Phase 4.5). No discovery candidates have been produced yet. See `docs/build_log.md` → "Phase 6 design notes (pin: SMTP secrets handoff)" for the related secret-management context and `docs/runbook_key_rotation.md` (doc 6 in Phase 6) for the Brave + Anthropic key rotation procedure.
