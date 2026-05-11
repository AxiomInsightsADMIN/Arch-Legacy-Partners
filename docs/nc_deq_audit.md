# NC DEQ Sub-Page Audit

**Date:** 2026-05-11 (Phase 2 step 2 first action)
**Author:** Axiom Insights
**Scope:** Per-page reconnaissance of NC DEQ DWR (Division of Water
Resources) and DWM (Division of Waste Management) program pages
relevant to the seven v1 facility categories, plus a robots.txt sweep
across NC DEQ application subdomains.

**No DB writes performed. No migration applied.**

---

## Headline findings

1. **NC DEQ's main site (`deq.nc.gov` / `www.deq.nc.gov`) is
   robots-permissive.** Standard Drupal `robots.txt` allowing /core/,
   /profiles/, /themes/ assets; only `/admin/`, `/search/`, `/user/*`,
   and the duplicates under `/index.php/` are Disallowed. Our content
   paths (`/about/divisions/...`) are fully allowed. **This is the
   opposite of TCEQ's posture** — NC DEQ does not blanket-disallow
   any application subdomain.
2. **NC DEQ publishes two master facility rosters as PDF/document
   downloads from the Solid Waste Facility Lists page:**
   - "Solid Waste Permitted Facilities" — transfer stations,
     composting, landfills, treatment & processing.
   - "Septage Firm" — registered septage firms (NC's equivalent of
     "septage hauler").
   Both rosters live on `edocs.deq.nc.gov` (NC DEQ's Laserfiche
   document repository). The DWM page at
   `/solid-waste-section/solid-waste-permitted-facility-information-and-guidance/solid-waste-facility-lists`
   links to both.
3. **`edocs.deq.nc.gov` is unreachable from my probe IP.** Three
   independent attempts to fetch the host (including a re-attempt
   after a 30-second timeout) all returned "Connection to
   edocs.deq.nc.gov timed out" — TCP connect failure, not a 4xx, not
   a 5xx, not a robots.txt issue. The host resolves via DNS but
   refuses the TCP handshake. This is **the dominant access
   constraint for NC** in v1 — the data is public, the linking page
   says so, but our scraper can't reach the document host from this
   network origin. Two paths around it (Playwright + a US-East IP,
   or a Phase-6 manual download by the client) discussed below.
4. **NC's residuals (Class A/B biosolids) program is NOT at any of
   the URLs we initially guessed.** It is reached through the
   "Non-Discharge Branch" landing and a `Permit Facility Map` page
   that hands off to an Esri ArcGIS Experience app at
   `experience.arcgis.com/experience/689283d17bf342c2a96364fbab09a5d8`.
   The map app is JS-driven; per-facility data is reachable via the
   underlying ArcGIS FeatureServer (likely on `services.nconemap.gov`
   — which **did** return a valid REST root with 46 services). NC
   OneMap is a publicly hosted geospatial portal with ArcGIS REST
   endpoints we can query without scraping HTML.

---

## Per-sub-page audit

### A. Working pages on `www.deq.nc.gov` (robots-permissive)

| Sub-page | URL | Format | What it carries | Category mapping |
|---|---|---|---|---|
| **Solid Waste Facility Lists** | `/about/divisions/waste-management/solid-waste-section/solid-waste-permitted-facility-information-and-guidance/solid-waste-facility-lists` | HTML index. Two edocs links to facility rosters. | Hub to "Solid Waste Permitted Facilities" PDF and "Septage Firm" PDF. | **5, 6 (partial), 7** via SW Permitted Facilities; **4** via Septage Firm |
| Solid Waste Permitted Facility Information + Guidance | `/about/divisions/waste-management/solid-waste-section/solid-waste-permitted-facility-information-and-guidance` | HTML hub. | Parent of the Facility Lists page and Annual Reporting page. | navigation only |
| Annual Facility + LG Reporting | `…/annual-reporting-local-government-and-solid-waste-facility-reporting` | HTML hub. 18 edocs links to per-program annual-report forms. | Compost AR, Transfer Station AR, MSW Landfill AR, etc. + a Laserfiche search URL that returns all FY2023-24 Annual Facility Reports. | corroboration for 5, 7 |
| DWM Composting program landing | `/about/divisions/waste-management/solid-waste-section/special-wastes-and-alternative-handling/composting` | HTML hub. | Composting rules + permit-application guidance PDFs (on edocs). No active-facility roster visible — that's in the SW Permitted Facilities doc. | 5 (via the master roster) |
| DWM Septage page | `…/special-wastes-and-alternative-handling/septage` | HTML hub. | Septage Management Rules + Food Service Memo (on edocs). No registered-firm roster visible — that's in the Septage Firm doc. | 4 (via the master roster) |
| Septage New Operator | `…/septage/septage-new-operator` | HTML hub. 17 edocs links. | NC septage facility taxonomy: SDTF (Septage Detention/Treatment Facility) and SLAS (Septage Land Application Site) — both forms-only on this page. | informational |
| DWR NPDES Wastewater | `/about/divisions/water-resources/water-quality-permitting/npdes-wastewater` | HTML hub. | NPDES program navigation. No state-level facility XLS exposed on the allowed path. NC TPDES-equivalent state-permit detail is gated behind NC's queryable interface we have not found a public-data XLS for. | **1** via ECHO (already loaded for NC) |
| DWR Non-Discharge Branch | `…/water-quality-permitting/non-discharge-branch` | HTML hub. | Program landing for residuals (biosolids land application) + reuse + spray irrigation. Points at the Permit Facility Map. | **3** via ArcGIS (below) |
| DWR Non-Discharge Permit Facility Map | `/about/divisions/water-resources/permitting/non-discharge/permit-facility-map` | HTML page hosting an ArcGIS Experience iframe. | The Experience app at `experience.arcgis.com/experience/689283d17bf342c2a96364fbab09a5d8` is a JS-driven facility map. Per-facility records are queryable via the underlying FeatureServer (NC OneMap). | **3** primary, **1** corroboration |
| DWR Collection Systems (PERCS) | `…/water-quality-permitting/collection-systems` | HTML hub. Cites `15A NCAC 2T` rule PDF. | Sewer-extension permits — relevant to POTW infrastructure but not a facility roster. Has a Crystal Reports link that may be the per-permit tracker. | informational |
| DWR NPDES Compliance + Enforcement | `…/npdes-wastewater/npdes-compliance-and-enforcement` | HTML hub. | Has a "List of current active SOCs" link to `/coastal-management/gis/data/...active-soc-list-07212021/download` (SOC = Special Order by Consent) — a permitted POTW enforcement-action dataset, dated July 2021. | corroboration |
| DWR Permits & Registration (alt index) | `/about/divisions/water-resources/permits-registration` | HTML hub. | All DWR permit programs in one list — useful as a discovery target for future NC source rows. | navigation only |

### B. ArcGIS / Esri assets discovered

**Initial assumption (round-1 audit, now corrected below):**

| Source | URL | Notes |
|---|---|---|
| Non-Discharge Permit Facility Map (Esri Experience) | `https://experience.arcgis.com/experience/689283d17bf342c2a96364fbab09a5d8` | JS-driven SPA. Reached via DWR Non-Discharge "Permit Facility Map" page. |
| NC OneMap REST root | `https://services.nconemap.gov/secure/rest/services` | Reachable. 46 services + 8 folders at root; 137 services counting folder contents. |

**FeatureServer discovery chain (Phase 2 step 2; documented here so
maintainers don't re-walk the chain if NC reshuffles their GIS
hosting):**

1. The DWR Non-Discharge **"Permit Facility Map"** page on
   `www.deq.nc.gov` embeds the Esri Experience SPA at
   `https://experience.arcgis.com/experience/689283d17bf342c2a96364fbab09a5d8`.
2. AGOL item metadata for that Experience
   (`https://www.arcgis.com/sharing/rest/content/items/<id>?f=json`)
   identifies it as item `689283d17bf342c2a96364fbab09a5d8` —
   *"DWR Locator Map (Public)"* owned by `DWR_GIS_Team`, hosted on
   the NC DEQ AGOL portal **`https://ncdenr.maps.arcgis.com`**.
3. The Experience's `/data?f=json` references a Web Map item
   `b200deee16ae417a931e10d96e9f2ac8` —
   *"Regional Office: All-in-One Map-withGroups-(Public)"*.
4. That Web Map references **44 FeatureServer/MapServer URLs**, all
   on `https://services2.arcgis.com/kCu40SDxsCGcuUWO/...` (the
   AGOL-hosted feature service host for NC DEQ's organization).
5. The Non-Discharge facility layer is at:
   ```
   https://services2.arcgis.com/kCu40SDxsCGcuUWO/arcgis/rest/services/
       NPDES_Non_Discharge_Permits_(View)/FeatureServer/0
   ```
6. Two adjacent layers in the same host worth knowing:
   - `Non_Discharge_Land_Application_Field_Permits_(View)/FeatureServer/0`
     — per-application-field detail (sub-record of the Non-Discharge
     permit). Not loaded in Phase 2 step 2; consider for Phase 5 if
     Phase 3 needs field-level resolution.
   - `NPDES_Wastewater_Discharge_Permits/FeatureServer/0` — NC's NPDES
     wastewater discharge permits. Corroborates EPA ECHO's NC POTW
     coverage. Not loaded; ECHO is already the primary path for
     category 1.

**Crucial correction to the round-1 assumption.** `NC1Map_Environment`
on `services.nconemap.gov` is the wrong service — it carries
forestry, estuarine habitat, and landslide layers, **not** any
DEQ facility data. The NC OneMap portal hosts statewide geospatial
foundation data (boundaries, hydrography, parcels, imagery); DEQ's
operational permit layers live on their own AGOL org at
`services2.arcgis.com/kCu40SDxsCGcuUWO`. Future maintainers adding
NC DEQ layers should look on the NC DEQ AGOL host first, not on NC
OneMap. The discovery script lives at `local/_nc_arcgis_probe*.py`
(round 1 through 4) — round 4 is the one that finally landed the
chain.

**Reusable lookup recipe** for any future NC DEQ AGOL-hosted layer:

```
1. Find the public-facing Experience URL on deq.nc.gov (it embeds
   experience.arcgis.com/experience/<32-char-id>).
2. GET https://www.arcgis.com/sharing/rest/content/items/<id>?f=json
   -> identifies the owner org (portal URL).
3. GET https://www.arcgis.com/sharing/rest/content/items/<id>/data?f=json
   -> the Experience config, which references the Web Map item id.
4. GET <org-portal>/sharing/rest/content/items/<webmap-id>/data?f=json
   -> dumps every FeatureServer/MapServer URL the Web Map uses.
5. The layer URL pattern is .../<service>/FeatureServer/<layer-id>.
```

The Esri FeatureServer pattern is well-suited for scraping: each
layer has a paged JSON query endpoint with standard semantics
(resultOffset, resultRecordCount, returnCountOnly, returnGeometry,
outSR). No browser needed, no JS-driven UI flow, no Playwright.

### C. The edocs.deq.nc.gov constraint

**Findings:**
- `edocs.deq.nc.gov` resolves via DNS but refuses TCP connections from
  the audit probe machine (three independent attempts, 30-second
  connect timeouts each).
- Both NC master rosters live on this host:
  - Solid Waste Permitted Facilities: `https://edocs.deq.nc.gov/WasteManagement/ElectronicFile.aspx?docid=2132701&dbid=0&repo=WasteManagement`
  - Septage Firm: `https://edocs.deq.nc.gov/WasteManagement/ElectronicFile.aspx?docid=2132702&dbid=0&repo=WasteManagement`
- Compost guidance PDFs and septage forms also live on edocs and
  return the same timeout pattern.
- I did **not** find a robots.txt for edocs (couldn't reach the host
  to fetch it) — so this is not a robots-disallow case; it is a
  network reachability case.

**Why edocs probably blocks us:**

The most likely explanation is a stateful firewall or WAF rule that
rejects connections that aren't from the State of NC's allow-listed
IP ranges, or that aren't preceded by a TLS / browser fingerprint
that matches a real desktop browser. Both are routine WAF behaviors;
neither implies the data is non-public — NC DEQ publishes the
linking page and the document IDs openly.

**Three forward paths:**

1. **Playwright + a US-East egress.** Same pattern we used for the
   CWNS APEX app. Playwright passes a real Chromium TLS fingerprint
   and runs from the same residential / commercial IP range as a
   normal user. If the constraint is anti-bot WAF + geofencing this
   gets us through.
2. **Manual one-off download by the client.** Arch Legacy Partners
   can fetch the two PDFs from a North Carolina office (or any IP
   that edocs accepts), forward them to Axiom Insights, and we
   ingest via a one-off migration with `extraction_method='manual'`.
   Same fallback pattern as the TCEQ Public Information Act path.
3. **Wait for re-test on the next monthly run.** Network constraints
   sometimes self-resolve (TLS fingerprint rotation, server
   throttling cools off, etc.). Worth re-probing in the Phase-5
   monthly cron the first time it runs.

**Recommendation: try Playwright first, fall back to manual
download.** That mirrors the discipline we used for CWNS APEX. If
Playwright works for edocs, it's also the canonical mechanism for
fetching the compost-program PDFs and septage forms when we eventually
want them.

### D. URLs that 404'd in initial recon

For future maintainers — these were guessed and don't exist on NC DEQ
today (URL structure has been refactored or the page never existed):

| URL guess | Status |
|---|---|
| `/about/divisions/water-resources/water-resources-permits/percs` | 404 — moved to `…/water-quality-permitting/collection-systems` |
| `/about/divisions/water-resources/water-quality-permitting/non-discharge-branch/residuals` | 404 — no residuals sub-page; reach via Non-Discharge landing |
| `/about/divisions/waste-management/solid-waste-section/permitted-solid-waste-facilities` and `/transfer-stations` | 404 — list is at `…/solid-waste-permitted-facility-information-and-guidance/solid-waste-facility-lists` |
| `/about/divisions/onsite-water-protection-section` | 404 — OSWPB is under DHHS not DEQ in NC; not in DEQ's tree |
| `/about/divisions/waste-management/solid-waste-section/landfills` and `/landfills-and-transfer-stations` | 404 — see Solid Waste Facility Lists |
| `/about/divisions/water-resources/permitting/non-discharge/residuals` | 404 |

**Subdomains probed:**

| Host | robots.txt status | Reachable? | Notes |
|---|---|---|---|
| `deq.nc.gov`, `www.deq.nc.gov` | **Permissive** (Drupal allow-with-admin-disallow) | yes | The primary site |
| `edocs.deq.nc.gov` | Unknown (host unreachable) | **No (TCP timeout)** | The document repo; gates the facility lists |
| `services.nconemap.gov` | (Not probed; ArcGIS REST has its own access semantics) | yes | NC OneMap REST — usable for FeatureServer queries |
| `experience.arcgis.com` | Esri's hosted SaaS; not in scope to scrape directly | yes | JS-driven Experience app |
| `reports.ncdenr.org` | (returned 200 with empty body on landing) | partially | Crystal Reports server — Sewer Extension Tracker referenced |
| `files.nc.gov` | 403 (Cloudflare/anti-bot) | no | NC state-wide file share; not relevant for our paths |
| `reports.nc.gov`, `ncdeq.imageware.com`, `energync.gov`, etc. | (Didn't resolve / not applicable) | no | Probed defensively; none are NC DEQ |

---

## ON-SITE WATER PROTECTION (OSWPB) — not in NC DEQ

NC's on-site wastewater regulation (septic systems and septage land
application from septic) is administered by **the NC Department of
Health and Human Services**, not NC DEQ. Specifically:

- NC DHHS → Division of Public Health → On-Site Water Protection
  Branch (OSWPB) — handles individual septic permits, soil evaluation,
  installer certifications.
- NC DEQ → DWM → Solid Waste Section → Septage Program — handles
  permitted septage firms (haulers) and SDTF / SLAS facilities.

So for v1 category 4 (private septage facilities), the relevant NC DEQ
page IS the SW Septage program we already audited (the "Septage Firm"
list on edocs). The OSWPB-administered side is septic-tank-installer
data, which is out of v1 scope.

This is documented here so the audit doesn't appear to have a hole;
OSWPB simply doesn't exist as a DEQ subdivision.

---

## Seven-category mapping (NC)

| Category | v1 plan |
|---|---|
| **1. POTW receiving stations** | EPA ECHO (already loaded for NC: 19,827 raw rows including 287 POTW). NC DEQ NPDES adds program metadata; no public XLS on the allowed path. Phase-4 enrichment for the "receiving station" subset. |
| **2. County manhole programs** | Not NC DEQ. Phase-2 county-level scrapers (NC county environmental health departments) + Phase-4.5 discovery. |
| **3. Land application sites** | NC DEQ DWR Non-Discharge program. Primary path: ArcGIS FeatureServer on NC OneMap (path TBD, layer needs identification). Secondary: residuals/biosolids facility list (Class A/B) likely on edocs. |
| **4. Private / regional septage facilities** | NC DEQ DWM "Septage Firm" master list. On edocs (currently unreachable from our IP — Playwright + manual fallback). |
| **5. Composting facilities** | NC DEQ DWM "Solid Waste Permitted Facilities" master list. On edocs (same access constraint). Also has annual-reporting data per facility. |
| **6. Anaerobic digesters** | NC DEQ DWM "Solid Waste Permitted Facilities" (partial — those with MSW-classified beneficial-gas-recovery permits). Standalone AD facilities require Phase-4.5 discovery. |
| **7. Transfer stations** | NC DEQ DWM "Solid Waste Permitted Facilities" master list. Same access constraint as 4 and 5. |

Three NC DEQ sources will give us coverage on **3, 4, 5, 6 (partial), 7** — five of seven categories. Category 1 (POTW) is delivered through ECHO already; category 2 (county manhole) is not NC DEQ.

---

## Proposed migration

A draft migration script at
[`supabase/migrations/20260511230000_nc_deq_subsource_seed.sql`](../supabase/migrations/20260511230000_nc_deq_subsource_seed.sql)
adds **three** new source rows:

| Slug | Type | base_url | tos_posture | robots_txt_status |
|---|---|---|---|---|
| `nc_deq_solid_waste_facility_list` | `state` | the edocs PDF URL (Solid Waste Permitted Facilities) | `permissive` | `allow` (on the linking page; edocs robots.txt unobserved due to TCP timeout) |
| `nc_deq_septage_firm_list` | `state` | the edocs PDF URL (Septage Firm) | `permissive` | `allow` (same as above) |
| `nc_deq_non_discharge_facilities` | `state` | the ArcGIS FeatureServer endpoint TBD (initial value: the Esri Experience URL; loader replaces with the specific FeatureServer once layer is identified) | `permissive` | `allow` (NC OneMap robots-permissive; FeatureServer semantics are public-by-design) |

I am **not** seeding rows for:
- `nc_deq_dwr_npdes_wastewater` (already covered by EPA ECHO for v1; adding a duplicate row with no scraper attached creates a maintenance burden)
- `nc_deq_dwr_collection_systems` (sewer-extension permits — not in our seven categories)
- `nc_deq_crystal_reports_sewer_tracker` (`reports.ncdenr.org`; requires deeper investigation to confirm public access)
- Composting and Septage program landings (program-doc hubs, not data sources)

If you want any of the above seeded for completeness, say so and I'll
add them with `tos_posture='unknown'` notes and no scraper bindings.

**Migration NOT applied.** Holding for review.

---

## v1 scope-limitations updates (NC-side)

The `docs/v1_scope_limitations.md` doc currently has a TX section. The
NC findings introduce **one** new v1 scope concession to note:

- **NC DEQ edocs document repository TCP-blocks our probe IP.** The
  data is public (linked from DEQ's permissive site); the document
  host has a network-level access constraint that vanilla HTTP fetch
  cannot pass. Categories affected: 3 (land application — secondary
  path), 4 (private septage), 5 (composting), 7 (transfer stations).
  v1 path: try Playwright first; fall back to manual one-off download
  by the client (analogous to the TX Public Information Act path).
  Phase-6 doc will include both the Playwright result (if successful)
  and a manual-fallback procedure if not.

I have **not yet appended** this to `v1_scope_limitations.md` — that
write is gated on your review of this audit. If you approve the
finding, I'll add it as a parallel NC section, framed the same way
as the TX one.

---

## Forensic artifacts (gitignored, kept locally for re-inspection)

All in `local/nc_deq_audit/` —

```
_summary.json, _summary_r2.json, _summary_r3.json  (round-by-round JSON)
+ 30+ saved HTML samples (one per probed sub-page)
```

---

## What needs review before next action

1. **Approve the proposed migration**
   `20260511230000_nc_deq_subsource_seed.sql` adding three NC source
   rows. After approval I'll apply it via supabase db push.
2. **Confirm the NC edocs access constraint write-up** for
   `docs/v1_scope_limitations.md` (parallel section to TX). After
   approval I'll append it.
3. **Choose the loader sequencing for NC.** Three sources, three
   distinct access paths:
   - `nc_deq_non_discharge_facilities` (NC OneMap ArcGIS FeatureServer)
     — easiest, no Playwright, no edocs blocker
   - `nc_deq_solid_waste_facility_list` (edocs PDF — needs Playwright
     for the access constraint, then pdfplumber for parsing)
   - `nc_deq_septage_firm_list` (same as above)
   
   Recommend building **ArcGIS first** to validate the NC OneMap path,
   then Playwright for the two edocs PDFs. ArcGIS gives us category 3
   coverage cleanly; the edocs work gives us 4 + 5 + 7.
4. **Confirm whether to seed
   `nc_deq_dwr_npdes_wastewater`** as a corroboration row even though
   EPA ECHO already covers NC POTWs. My default is no; raise if you
   want it included.

Holding for your review.
