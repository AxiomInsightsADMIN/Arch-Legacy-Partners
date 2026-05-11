# TCEQ Public Data Lookup — Sub-Page Audit

**Date:** 2026-05-11 (Phase 2 first action)
**Author:** Axiom Insights
**Scope:** Sub-page reconnaissance of every PDL link relevant to the seven
v1 facility categories. Output is a forward plan for which TCEQ sub-pages
get a dedicated source row in `source` (for drift-detection granularity),
which get scraped, and which are declined with reason.
**No DB writes performed. No migration applied.**

## Headline findings

1. **The MSW Data hub publishes a refreshed-weekly XLS at a static URL
   on `www.tceq.texas.gov`.** `msw-facilities-texas.xls` (824 KB, BIFF
   binary format with magic `D0 CF 11 E0`) lists every issued/acknowledged
   MSW permit and registration plus pending applications, with facility
   name and type, permit number, authorization status, and physical
   location. This is the **primary Phase-2 TCEQ data source for
   categories 5 (Composting), 6 (Anaerobic Digesters — to the extent
   AD facilities carry MSW classification), and 7 (Transfer
   Stations).** Refreshed every Friday morning per the page text.
2. **Every TCEQ application subdomain (`www2`, `www3`, `www6`, `www15`,
   `www18`) carries the same blanket `User-agent: * / Disallow: /` as
   CRPUB.** That is **all** the queryable interfaces — WQPAQ (TPDES
   permits), WQ-DPA (general permits), WWPS (plans + specs), STEERS
   (e-permitting), CRPUB (registry). Per locked decision 8.12, none
   are scraped. Only `www.tceq.texas.gov` static content is in-scope
   for v1.
3. **No public registry of Sludge / Septage transporters exists on
   `www.tceq.texas.gov`.** The `Sludge Transporter Reporting` page
   covers reporting *requirements* (annual summary form TCEQ-00316,
   guide RG-309) but not a list of registered transporters. The list
   itself sits in CRPUB (robots-disallowed). Phase-4.5 discovery
   crawl is the natural fit for category-4 coverage.
4. **No public XLS of POTW permits / sludge land-application
   registrations on `www.tceq.texas.gov`.** Wastewater + Sludge program
   landings have process docs and forms (PDF), but no per-state bulk
   data files in the `/assets/public/` tree. Texas POTW coverage is
   delivered via EPA ECHO (already in `raw_facility_record`).
   Category-3 (land application) coverage is sparse on the TCEQ side
   and Phase-4.5 discovery is the realistic path.

The robots-disallow blanket across subdomains is the dominant
architectural constraint. v1 Texas coverage will lean on:

- EPA ECHO (federal, already loaded) for POTWs + CWA-permitted
  facilities including some biosolids land-application sites.
- TCEQ MSW XLS (this audit) for transfer/composting/AD with MSW
  classification.
- Phase-4.5 discovery crawl for septage haulers, private septage
  facilities, county manhole programs.

---

## Per-sub-page audit

For each sub-page: URL, format, observed row/asset count, fields if
visible, robots posture, and 7-category mapping.

### A. ACTIVE — sources we *will* scrape

#### A1. MSW Active Facilities XLS

| | |
|---|---|
| **Proposed slug** | `tceq_msw_facilities_xls` |
| **Entrypoint** | `https://www.tceq.texas.gov/permitting/waste_permits/msw_permits/msw-data` |
| **Data file URL** | `https://www.tceq.texas.gov/assets/public/permitting/waste/msw/msw-facilities-texas.xls` |
| **Format** | Old binary `.xls` (BIFF, CFB header `D0 CF 11 E0 A1 B1 1A E1`). 824 KB. Parses with `xlrd>=2.0.1`. |
| **Refresh cadence** | "Each Friday morning" per the page text. |
| **Field set** | Per the page: *facility name and type; permit, registration, or notification number; authorization status; facility physical status; and location information.* Schema reference: GI-613 publication (`gi-613-description-of-fields-msw-data-files.pdf`). |
| **Row count** | Not yet parsed — pandas xlrd not installed. (Proposed dependency below.) The XLS is 824 KB; rough estimate is 5,000-10,000 facility rows. |
| **ToS posture** | Permissive — public records, static asset, no auth required. |
| **robots.txt** | `www.tceq.texas.gov` — Allow (with `/search`, `/@@search` Disallows that don't apply here). |
| **Feeds categories** | **5 (Composting)**, **6 (Anaerobic Digester, partial — MSW-permitted ADs only)**, **7 (Transfer Stations)** |
| **Loader plan** | Fetch the XLS, parse with pandas, upsert per-facility row into `raw_facility_record` with `source_record_id` = TCEQ permit/registration number. Same drift-signature pattern as ECHO + CWNS. |

#### A2. MSW Closed Facilities XLS (lower priority — historical)

| | |
|---|---|
| **Proposed slug** | `tceq_msw_closed_facilities_xls` |
| **Data file URL** | `https://www.tceq.texas.gov/assets/public/permitting/waste/msw/msw-closed-facilities-texas.xls` |
| **Format** | Same BIFF .xls; same schema as A1. |
| **ToS / robots** | Same as A1 — permissive / allow. |
| **Feeds categories** | Historical reference only. Not loaded in v1; documented so a future cycle can opt in. |
| **Decision** | **Defer to Phase 6 if needed.** Not seeded in this migration. |

(Note: a third file `msw-revoked-or-not-issued-texas.xls` exists for
revoked/denied permits. Same disposition — not in v1.)

### B. DECLINED — robots-disallowed query interfaces

Every `wwN.tceq.texas.gov` subdomain returned an identical 28-byte
`robots.txt`:

```
User-agent: *
Disallow: /
```

Confirmed live for `www2`, `www3`, `www6`, `www15`, `www18`. Per locked
decision 8.12, we honor the disallow on all of them.

| Slug (NOT seeded) | Host | URL | Disallow reason |
|---|---|---|---|
| `tceq_wqpaq` | `www6.tceq.texas.gov` | `/wqpaq/index.cfm` (WQ Basic Permit Search) | `robots.txt: Disallow: /` |
| `tceq_wq_dpa` | `www2.tceq.texas.gov` | `/wq_dpa/index.cfm` (WQ General Permits Search) | `robots.txt: Disallow: /` |
| `tceq_wwps` | `www18.tceq.texas.gov` | `/wwps/` (WWPS — Wastewater Plans + Specs tracker) | `robots.txt: Disallow: /` |
| `tceq_steers` | `www3.tceq.texas.gov` | `/steers/` (STEERS e-permitting) | `robots.txt: Disallow: /` |
| `tceq_central_registry` (already in seed) | `www15.tceq.texas.gov` | `/crpub/` | `robots.txt: Disallow: /` |

**Implication for v1 coverage:** TPDES (POTW state-level) permit detail
is unreachable except via ECHO's NPDES rollup. We have all the
*facilities* through ECHO already; missing is some TX-state permit
metadata (e.g. permit issuance/expiration dates, regional office). For
v1 we proceed without that metadata; Phase 6 may revisit via TCEQ open
records request.

### C. PROGRAM LANDING PAGES — no scrape target on them

These exist as static HTML at `www.tceq.texas.gov` and pass robots, but
they do **not** publish facility lists or bulk data files. They are
process documentation. Per Ryan's "each TCEQ sub-page we actually
scrape needs its own source slug for drift detection granularity" —
since we don't scrape these, they don't get individual slugs. The
existing `tceq_public_data_lookup` (umbrella catalog) and
`tceq_domestic_wastewater` (program landing) rows in `0002_source_seed`
remain appropriate for the umbrella references.

| URL | Title | Why no scrape |
|---|---|---|
| `/agency/data/lookup-data` | PDL umbrella catalog | Hub page; no data files. Existing seed row covers it. |
| `/agency/data/lookup-data/lookup-data/waste-mgmt-data-records.html` | Waste Mgmt Permits/Registrations/Facilities | Hub page; the actual data is at the MSW Data hub (A1). |
| `/agency/data/lookup-data/status-stormwater-wastewater.html` | Stormwater + Wastewater status hub | All sub-links go to robots-disallowed subdomains. |
| `/permitting/wastewater/municipal` | Municipal Domestic Wastewater landing | Process docs; no facility list. ECHO covers POTWs. |
| `/permitting/wastewater/municipal/WQ_Domestic_Wastewater_Permits.html` | Municipal Wastewater Permits (process steps) | Documentation. |
| `/permitting/wastewater/sludge/WQ_sludge_ClassB_forms.html` | Class B Biosolids Land Application Permits | Forms (PDF), not a registry. |
| `/permitting/wastewater/sludge/WQ_sludge_septage_forms.html` | Domestic Septage Land Application Registrations | Forms (PDF), not a registry. |
| `/permitting/wastewater/sludge/sludge-biosolids` | Sludge/Biosolids program home | Navigation page. |
| `/permitting/wastewater/sludge/WQ_sludge_AIR.html` | "Am I Regulated?" Sludge/Biosolids | Self-assessment guide. |
| `/permitting/wastewater/sludge/WQ_sludge_ClassA_forms.html` | Class A/AB Biosolids notification forms | Forms (PDF), not a registry. |
| `/permitting/registration/sludge/Am_I_Regulated.html` | Sludge Transporters: Am I Regulated? | Self-assessment guide. |
| `/permitting/registration/sludge/reporting.html` | Sludge Transporter Reporting | Reporting requirements + annual summary form TCEQ-00316. **Not a public registry of registered transporters.** |
| `/permitting/wastewater/sludge/WQ_sludge_reporting.html` | Annual / Quarterly Reports for Class B Biosolids | Documentation page. |

### D. URLs that 404'd in initial recon

Documented for future maintainers to know we checked them:

| URL guess | Status |
|---|---|
| `/permitting/waste_permits/msw_permits/msw-transporter` | 404 — does not exist. MSW transporter program lives under solid-waste registration elsewhere. |
| `/permitting/waste_permits/msw_permits/msw_permitted_facilities` | 404. The real entrypoint is `/msw-data` (A1). |
| `/permitting/waste_permits/msw_permits/msw-permitted-facilities.html` | 404. Same as above. |
| `/permitting/waste_permits/msw_permits/composting`, `…/composting.html` | 404. Composting is rolled into the MSW XLS via facility-type codes, not a separate sub-page. |
| `/permitting/wastewater/sludge/WQ_sludge_transporter_landing.html`, `/sludge_transporter.html` | 404. Sludge transporter info lives under `/permitting/registration/sludge/`. |

---

## Seven-category mapping (revised after this audit)

| Category | Source plan for v1 |
|---|---|
| **1. POTW receiving stations** | EPA ECHO (loaded). TCEQ adds nothing on the allowed path; TPDES permit detail lives in WQPAQ which is robots-disallowed. |
| **2. County manhole programs** | Not TCEQ. Phase 2 county-level scrapers and Phase-4.5 discovery. |
| **3. Land application sites** | EPA ECHO (partial — surface-discharge biosolids only). TCEQ has no public XLS. Phase-4.5 discovery + Phase-6 TCEQ open records request as fallback. |
| **4. Private / regional septage facilities** | No TCEQ public source. Phase-4.5 discovery crawl (Brave Search + Haiku extraction) is the canonical v1 path. |
| **5. Composting facilities** | **TCEQ MSW XLS (slug `tceq_msw_facilities_xls`)** — covers TCEQ-permitted composting and processing facilities. Phase-4.5 discovery for non-permitted. |
| **6. Anaerobic digesters** | TCEQ MSW XLS (partial — MSW-permitted ADs only). ECHO (partial — surface-discharge biogas). Phase-4.5 discovery for standalone ADs. |
| **7. Transfer stations** | **TCEQ MSW XLS** — primary. Phase-4.5 discovery for private/specialized. |

Only **categories 5 and 7** get meaningful new coverage from the TCEQ
sub-source proposed below. Category 6 gets partial coverage. Categories
1, 3, 4 are unchanged from the ECHO + planned-discovery state.

---

## Proposed migration

A draft migration script is at
[`supabase/migrations/20260511220000_tceq_subsource_seed.sql`](../supabase/migrations/20260511220000_tceq_subsource_seed.sql)
adding **one** new source row:

| Slug | Type | base_url | tos_posture | robots_txt_status |
|---|---|---|---|---|
| `tceq_msw_facilities_xls` | `state` | the XLS asset URL | `permissive` | `allow` |

I deliberately did **not** add rows for the declined query subdomains
(`tceq_wqpaq`, `tceq_wq_dpa`, `tceq_wwps`, `tceq_steers`). Reasoning:
they're documented in this audit and in the build log, but adding them
to `source` would imply a future scrape obligation. Per Ryan's
instruction *"each TCEQ sub-page we actually scrape needs its own source
slug"* — we don't scrape these, so they don't get slugs.

The closed-facilities and revoked/not-issued XLS files are also not
seeded in v1. They're documented above for a future migration if
priorities change.

**Migration NOT applied.** Holding for Ryan's review.

---

## Proposed new dependency: `xlrd>=2.0.1`

The MSW XLS at A1 is in old BIFF binary format. `pandas.read_excel`
requires `xlrd>=2.0.1` for `.xls` parsing (the modern openpyxl engine
only handles `.xlsx`). This adds one Python dependency to
`requirements.txt`.

Alternative considered: convert the .xls to .xlsx server-side using
some intermediate tool. Rejected — `xlrd` is a small, mature library
(it dates to 2005 and has stable maintenance) and avoids the
intermediate-format step. Adding it now is a smaller surface change
than re-engineering the loader later.

**xlrd NOT yet added to requirements.txt.** Holding for Ryan's
approval.

---

## Robots.txt observations (raw)

```
www.tceq.texas.gov:
  Sitemap: https://www.tceq.texas.gov/sitemap.xml.gz
  User-agent: *
  Disallow: /@@tceq-search
  Disallow: /@@search
  Disallow: /search
  Disallow: /tceq-search
  Disallow: /assistance/resources/the-advocate-1/the-advocate-water-articles
  Disallow: /assistance/industry/pst/natural-disaster-recovery-field-screening-checklist-and-abandoned-ust-field-documentation
  Disallow: /portal_vocabularies
  (+ Googlebot-specific rules that don't apply to our UA)

www2.tceq.texas.gov:   User-agent: * / Disallow: /
www3.tceq.texas.gov:   User-agent: * / Disallow: /
www6.tceq.texas.gov:   User-agent: * / Disallow: /
www15.tceq.texas.gov:  User-agent: * / Disallow: /   (already documented for CRPUB)
www18.tceq.texas.gov:  User-agent: * / Disallow: /
```

Our target paths (`/assets/public/...`, `/permitting/...`, `/agency/data/...`,
`/downloads/...`) are NOT in any of the `www.tceq.texas.gov` Disallow
patterns. All non-`www` subdomains are blanket disallowed and excluded
from v1 scope.

---

## Forensic artifacts (gitignored, kept locally for re-inspection)

```
local/tceq_audit/
├── _summary.json              # round 1 audit JSON
├── _summary_r2.json           # round 2 audit JSON
├── msw-facilities-texas.xls   # the MSW XLS (824 KB)
├── pdl_umbrella_catalog.html
├── waste_mgmt_data_records_top.html
├── stormwater_wastewater_status.html
├── domestic_wastewater_program_landing.html
├── sludge_class_b_program_landing.html
├── sludge_program_am_i_regulated.html
├── r2_msw_data_hub.html
├── r2_septage_land_application_registration.html
├── r2_sludge_biosolids_home.html
├── r2_class_a_ab_biosolids_forms.html
├── r2_goto_wqpaq_wq_individual_permit_apps_status.html
├── r2_goto_wq_dpa_wq_general_permit_authorizations_status.html
├── r2_goto_wwps_wastewater_plans_specs.html
├── r2_goto_centralregistry.html
├── r2_municipal_wastewater_permits_process_steps.html
├── r2_wastewater_treatment_landing.html
├── r3_sludge_transporters_reporting_am_i_regulated_html.html
├── r3_sludge_transporters_reporting_reporting_html.html
└── r3_wqpaq_index.html
```

---

## What needs Ryan's review before next action

1. **Approve the proposed migration** `20260511220000_tceq_subsource_seed.sql`
   adding `tceq_msw_facilities_xls`. If approved, I'll apply it to
   Supabase via `supabase db push --db-url <pooler>` (same flow as the
   Phase-1 migrations).
2. **Approve the new dependency `xlrd>=2.0.1`** in `requirements.txt`.
   Single library; needed for parsing the MSW BIFF .xls.
3. **Confirm v1 scope decisions** above — specifically:
   - Category 4 (private septage) is deferred to Phase-4.5 discovery
   - Category 3 (land application) is deferred (Phase-4.5 + Phase-6)
   - Categories 1 (POTW) state-permit detail is deferred (Phase-6 open
     records request if needed)

After your nod on these three items I'll: apply the migration,
add the dependency to `requirements.txt`, then build
`scrapers/state/tceq_msw_xls.py` (parallel layout to
`scrapers/federal/epa_*.py`) as the first Phase-2 scraper.
