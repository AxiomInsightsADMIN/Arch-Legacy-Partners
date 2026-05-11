# v1 Scope Limitations

**Status:** Phase 6 deliverable (final-handoff package). Authored Phase 2
day 1 so the limitations are documented as we encounter them, not
retrofitted at the end.

**Audience:** Arch Legacy Partners operations team (primary), Axiom
Insights maintainers (secondary), any external reviewer (auditor or
counsel) evaluating the v1 dataset's coverage.

The Phase-1 brief and the Phase-2 source audit (`docs/tceq_pdl_audit.md`)
identified specific upstream data access constraints that bound the v1
delivery. This document records each constraint, names the affected
facility categories, and lays out the alternative path through which the
client can fill the gap post-delivery.

---

## 1. TCEQ application-subdomain robots.txt disallow

**Finding.** Every Texas Commission on Environmental Quality (TCEQ)
application subdomain returns the same blanket `robots.txt`:

```
User-agent: *
Disallow: /
```

(28 bytes, identical across all five subdomains; verified live on
2026-05-11.)

**Affected hosts and the data they each gate**

| Host | Application | What lives there |
|---|---|---|
| `www2.tceq.texas.gov` | WQ-DPA | TCEQ Water-Quality General Permit Authorizations and Applications status search (TPDES coverages by Notice of Intent, ag operations, stormwater). |
| `www3.tceq.texas.gov` | STEERS | TCEQ State of Texas Environmental Electronic Reporting System — the e-permitting portal used by applicants and operators for new permits, renewals, and reporting. |
| `www6.tceq.texas.gov` | WQPAQ | Water-Quality Individual Permit Applications Status — the queryable interface for TPDES individual permits including municipal domestic wastewater (POTW) permits and biosolids land-application permits. |
| `www15.tceq.texas.gov` | CRPUB | TCEQ Central Registry public-facing search — the master facility / regulated-entity lookup. |
| `www18.tceq.texas.gov` | WWPS | Wastewater Plans & Specifications approval tracker. |

**Locked decision applied.** Architectural decision 8.12 of the kickoff
brief requires us to honor the robots.txt declaration. Sources we
choose not to scrape are documented with a reason. All five hosts above
are documented as declined; the affected source rows in the `source`
table either carry `robots_txt_status='disallow'` (`tceq_central_registry`)
or are not seeded at all (`tceq_wqpaq`, `tceq_wq_dpa`, `tceq_wwps`,
`tceq_steers` — see audit doc).

## 2. v1 categories affected

For the seven facility categories the v1 build covers, the TCEQ
restriction bites in three places:

### Category 1 — POTW receiving stations (Texas)

**What we have.** EPA ECHO's CWA REST API delivered 72,499 active TX
NPDES-permitted facilities, including the POTW subset
(`CWPFacilityTypeIndicator='POTW'`) — that's 1,565 Texas POTWs. The
canonical fields the contract requires (name, address, lat/long,
permit IDs) are all present.

**What is unreachable in v1.** TCEQ's state-specific TPDES permit
metadata that exists *only* in WQPAQ — issuance / expiration dates,
permit-specific design flow, regional office assignment, and the
"receiving station" operational flag (i.e. which POTWs accept hauled
septage). EPA ECHO's CWA filter reports treatment classification
(POTW vs NON-POTW) but not whether the POTW operates a manifested-load
hauler receiving station.

**Coverage impact.** v1 lists every Texas POTW. The subset that
specifically functions as a *receiving station* is enriched via the
Phase 4 LLM acceptance-flag pass and the Phase 4.5 discovery crawl
(Brave Search + Haiku extraction of operator websites and county
hauler-program pages). The acceptance flags `accepts_septage`,
`accepts_grease_trap`, and `accepts_portable_toilet` start at
`'Unknown'` and are upgraded as evidence is found.

### Category 3 — Land application sites (Texas)

**What we have.** EPA ECHO covers the subset of land-application
sites that hold surface-water-discharge permits (typically large
Class A biosolids facilities and POTW-affiliated sites).

**What is unreachable in v1.** The TCEQ Class B Biosolids and
Domestic Septage land-application registry. TCEQ has program
landings under `/permitting/wastewater/sludge/` but does not publish
a per-state public XLS of registered land-application sites. The
registry data lives in CRPUB (robots-disallowed).

**Coverage impact.** Texas Class B / Domestic Septage land-application
coverage is partial in v1. Phase 4.5 discovery surfaces additional
sites via Brave Search; Phase 6 may close the gap via a Texas Public
Information Act request (see section 3 below) if the client wants a
provably-complete TX biosolids/septage land-application list.

### Category 4 — Private / regional septage facilities (Texas)

**What we have.** Nothing from TCEQ. The TCEQ Sludge Transporter
program is documented at `/permitting/registration/sludge/` but those
pages describe reporting *requirements* and forms (e.g. Form
TCEQ-00316 Annual Summary Report, publication RG-309) — they are not
a public registry of registered transporters. The list of registered
transporters lives in CRPUB (robots-disallowed).

**Coverage impact.** Texas private / regional septage facility
coverage in v1 relies entirely on Phase 4.5 discovery crawl (Brave
Search + Haiku extraction of operator websites) and on Phase 2
county-level scrapers where county health departments publish
hauler-program lists. This is the *canonical* v1 path for category
4 in Texas — it is not a "fallback"; it is how that category is built.

### Categories 5 / 7 (and 6, partial)

**Not constrained.** The TCEQ MSW XLS at
`www.tceq.texas.gov/assets/public/permitting/waste/msw/msw-facilities-texas.xls`
is robots-permissive and publishes 1,494 active MSW facilities
(transfer stations, composting, processing, landfills). That covers
v1 categories 5 (composting) and 7 (transfer stations) cleanly, and
the MSW-classified subset of category 6 (anaerobic digesters with
MSW permits). Standalone non-MSW anaerobic digesters fall to Phase
4.5 discovery.

---

## 3. Alternative path for the client: Texas Public Information Act

If post-delivery the client (Arch Legacy Partners) needs the data
that v1 cannot pull through automated means, the supported path is a
**Texas Public Information Act (TPIA)** request to TCEQ. Texas has
one of the more responsive sunshine regimes in the country and TCEQ's
records-services office routinely fulfills bulk-data requests for
permits and registrations.

### How an Arch Legacy Partners team member files a request

1. **Form.** TCEQ accepts requests by letter, email, or via the agency
   web form at the page titled *"Public Information Act Requests"*
   reached from `https://www.tceq.texas.gov/agency/data/records-services`.
   Letter or email is generally preferred for bulk-data requests
   because it produces a paper trail and a stable request ID.

2. **Required content of the request.**
   - The requester's name and contact information.
   - A specific description of the records sought. For category 1:
     "All currently active TPDES Domestic Wastewater Discharge permits,
     including permit number, permittee, facility address, design flow,
     issuance date, expiration date, and regional office, as a
     spreadsheet (Excel or CSV)." For category 3: "All currently
     active Class B Biosolids land-application sites and Domestic
     Septage land-application registrations, including site name,
     legal land descriptor, permit / registration number, county, and
     active status." For category 4: "The current list of registered
     sludge / domestic-septage transporters in Texas, including
     registrant name, registration number, county of operation, and
     active status."
   - A preferred format (electronic / spreadsheet).
   - A statement that the request is for non-commercial public-interest
     research / facility-database maintenance, if the client wants the
     fees waived (TCEQ has a statutory framework for fee waivers).

3. **Timeline.** TCEQ acknowledges within 10 business days under the
   Public Information Act; typical fulfillment for bulk data is 2–6
   weeks depending on the office's queue and whether redactions are
   required. Most facility-registry data has no redactable PII.

4. **Where to send.**
   - Email: `recordsmanager@tceq.texas.gov` (TCEQ Records-Services).
   - Postal: TCEQ Records Manager, MC-199, P.O. Box 13087, Austin TX
     78711-3087.
   - Online portal (when available): the page above includes an
     intake form.

5. **What to do with the response.** TCEQ typically returns the data
   as an Excel workbook. The client forwards it to Axiom Insights;
   we re-shape it into a one-off migration that adds the records to
   `raw_facility_record` with a new source row (e.g.
   `tceq_pia_<topic>_<yyyymmdd>`). The migration carries explicit
   provenance: `extraction_method='manual'`, `confidence='high'`,
   `source_date=<TPIA fulfillment date>`. Same canonical-resolution
   rules apply.

### When a TPIA request is worth filing

In rough priority order for the client to weigh:

| Category | Default v1 path | When to file TPIA instead |
|---|---|---|
| 4 (Private septage) | Phase 4.5 discovery | When the discovery crawl is suspected of low recall in TX — e.g. small-operator coverage is sparse. The TCEQ transporter registry is the single canonical list. |
| 3 (Land application) | ECHO + Phase 4.5 discovery | When the client needs a provably-complete Texas biosolids land-application inventory — e.g. for a regulatory submission or due-diligence pass. |
| 1 (POTW receiving) | ECHO + Phase 4 enrichment | When the client needs state-issued TPDES permit metadata beyond what ECHO surfaces — e.g. design-flow numbers, regional office, regional contact. |

In Phase 6 we will hand the client this document plus a short
checklist that maps each category's TPIA request template to the data
they would receive back. The client can file independently or
contract us to file on their behalf.

---

## 4. NC DEQ edocs document-repo TCP block

**Finding.** NC Department of Environmental Quality publishes the two
master facility rosters that gate v1 NC coverage on their Laserfiche
document repository at `edocs.deq.nc.gov`:

- "Solid Waste Permitted Facilities" — transfer stations, composting,
  landfills, and treatment/processing operations.
- "Septage Firm" — registered septage firms (NC's haulers + SDTF +
  SLAS operators).

The linking page at
`https://www.deq.nc.gov/about/divisions/waste-management/solid-waste-section/solid-waste-permitted-facility-information-and-guidance/solid-waste-facility-lists`
is fully robots-permissive. The host of the documents themselves —
`edocs.deq.nc.gov` — is reachable via DNS but **refuses TCP
connections from our probe IP**. Three independent fetch attempts on
2026-05-11 returned 30-second connect timeouts. This is **not** a
robots.txt issue (the host doesn't respond enough to serve a
`robots.txt` either); it is a network-level constraint, likely a
geofencing or anti-bot WAF rule.

**Affected categories for NC**

| Category | Path that's blocked | Coverage impact |
|---|---|---|
| 3 (Land application sites) | Secondary path — the SLAS subset of the Septage Firm list and any biosolids residuals on the SW facilities list | Mitigated: ArcGIS-based primary path (NC OneMap Non-Discharge FeatureServer) is unaffected |
| **4 (Private / regional septage)** | **Primary path — Septage Firm master list** | Significant — no allowed-path alternative on the main site |
| **5 (Composting)** | **Primary path — SW Permitted Facilities master list** | Significant — Phase-4.5 discovery + EPA ECHO partial corroboration are the alternatives |
| **6 (Anaerobic digesters)** | Primary path — SW Permitted Facilities (beneficial-gas-recovery permits) | Mitigated: ECHO partial + Phase-4.5 discovery |
| **7 (Transfer stations)** | **Primary path — SW Permitted Facilities master list** | Significant — no allowed-path alternative on the main site |

**Forward path: Playwright + manual fallback**

The constraint pattern matches what we saw with the EPA CWNS APEX app
(programmatic access blocked even though a real browser can reach
the host). The Phase-2 NC loader for the two edocs PDFs will use
Playwright + Chromium first (browser TLS fingerprint typically passes
WAF rules that vanilla `requests` fails), exactly the way the Phase-1
CWNS loader does for the APEX state-zip flow.

If Playwright is also blocked (e.g., the WAF rule is IP-based, not
fingerprint-based), the fallback for the client is a manual one-off
download from a North-Carolina or US-East egress, mirroring the
Texas Public Information Act path described in section 3 above:

1. **Where the documents live (linking page).**
   `https://www.deq.nc.gov/about/divisions/waste-management/solid-waste-section/solid-waste-permitted-facility-information-and-guidance/solid-waste-facility-lists`
2. **The two specific document URLs:**
   - SW Permitted Facilities (docid 2132701):
     `https://edocs.deq.nc.gov/WasteManagement/ElectronicFile.aspx?docid=2132701&dbid=0&repo=WasteManagement`
   - Septage Firm (docid 2132702):
     `https://edocs.deq.nc.gov/WasteManagement/ElectronicFile.aspx?docid=2132702&dbid=0&repo=WasteManagement`
3. **Manual procedure if Playwright fails.** A client team member with
   access from any IP that edocs accepts opens both URLs, downloads the
   PDFs (or whatever format edocs returns), forwards them to Axiom
   Insights, and we ingest via a one-off migration with
   `extraction_method='manual'` on each row's `field_provenance`. Same
   ingestion shape as the Texas TPIA-response path. No special
   credentials required from the client; this is public-record data.
4. **Re-test cadence.** Phase-5 monthly cron will re-probe edocs on
   each refresh. Network constraints sometimes self-resolve (WAF
   fingerprint rotation, throttling cooldown, etc.). If the network
   constraint disappears, the loader silently transitions back to
   automated fetch with no migration needed.

**What is NOT affected on the NC side**

- EPA ECHO's NC POTW coverage (19,827 raw rows; 287 POTW) is already
  loaded and is unrelated to edocs.
- The NC OneMap ArcGIS FeatureServer at `services.nconemap.gov` is
  reachable and serves the Non-Discharge facility map data without
  Playwright. Category 3 v1 coverage in NC is delivered via this
  path.

This is a **smaller** v1 concession than the TX one. TX lost
state-level POTW permit detail and the entire Septage Transporter
registry from automated paths. NC's situation is narrower: the
documents are public and linked, the access is just gated at the
network layer, and Playwright is a credible first try.

## 5. Non-TCEQ / non-NC analogues — when this matters for other states

The TCEQ pattern (open static-XLS + locked-down query subdomains) is
specific to Texas. The NC DEQ pattern (open document references +
gated document host) is specific to North Carolina. Each new state
added after v1 delivery gets its own source audit. **Neither the TX
nor the NC concession in this document automatically applies to other
states.** Each state's audit is its own document.

---

## 6. Forward roadmap (out of v1 scope)

These items are out of v1 scope but worth noting so the client knows
the gap is not permanent:

- **Phase 4** (LLM enrichment, Day 6-8): the three acceptance flags
  (`accepts_septage`, `accepts_grease_trap`, `accepts_portable_toilet`)
  for every canonical_facility row are populated by an Anthropic
  Haiku pass over operator-site and county-hauler-program content.
- **Phase 4.5** (Discovery crawl, Day 8-10): Brave Search +
  Haiku-extraction crawl finds net-new facilities not in any
  scraped source. Per-state bounded query budget; net-new entries
  are gated through `discovery_review_queue` before promotion.
- **Phase 6** (Days 11-12): final-handoff documentation includes
  this document plus a TPIA-request checklist per Texas category
  the client may want to backfill.
- **Post-delivery $40/state additions**: each new state ships its
  own source audit, its own scope-limitations entry, and its own
  loader build cycle.

---

## 7. Audit trail

This document is the canonical reference for the items above. The
chronology lives in `docs/build_log.md`. The TCEQ audit detail lives
in `docs/tceq_pdl_audit.md`. The NC DEQ audit detail lives in
`docs/nc_deq_audit.md`. Any future v1-scope concession discovered
during Phase 2–5 will be appended as a new top-level section here,
not written separately, so the client gets a single page of
constraints at handoff.
