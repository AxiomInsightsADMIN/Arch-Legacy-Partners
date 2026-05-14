# Data Quality Scorecard — v1
Per state × per category snapshot of the canonical dataset as of the latest Supabase state. This is what Austin's team uses to trust the data on Day 1; it shows the baseline that lands in the monthly refresh CSV.
**Generated:** live from Supabase via `local/_build_scorecard.py`; numbers are not estimates. Re-run the script to refresh this doc after any resolver or enrichment pass.
**In-scope definition:** `canonical_facility.facility_type IS NOT NULL` with the discovery-crawl review-queue gate applied — i.e. the row set returned by `v_all_in_scope`.

---

## 1. Top-level totals

| Metric | Count |
|---|---:|
| `v_all_in_scope` (customer-facing) | **1,970** |
| Total `canonical_facility` rows | 73,054 |
| NULL `facility_type` (out of v1 scope by design) | 70,982 |
| Discovery-crawl canonicals **held** by review-queue gate | 102 |
| Discovery review-queue rows **pending** adjudication | 141 |

The customer-facing dataset is `v_all_in_scope`. The held-discovery subset (above) is in `canonical_facility` but not in the view; it becomes visible row-by-row as Ryan approves each via the workflow in `docs/runbook_review_queue.md`.

## 2. Per-state × per-category breakdown

Includes geocoded coverage, acceptance-flag commitment rates (Phase 4 Haiku 4.5 v1.1.1 prompt; see `docs/build_log.md` 2026-05-14 Phase 4 close), and contact-field population. Acceptance commit-rate counts `Yes` and `No` as committed; `Unknown` is the honest-abstention default.

### TX

| Category | n | Geocoded | Septage Y/N/Unk | Grease Y/N/Unk | Porta Y/N/Unk | Phone | Email | Website |
|---|---:|---:|---|---|---|---:|---:|---:|
| `private_regional_septage_facility` | 36 | 36 (100.0%) | 18/0/18 | 16/1/19 | 5/0/31 | 0.0% | 0.0% | 0.0% |
| `potw_receiving_station` | 0 | — | — | — | — | — | — | — |
| `land_application_site` | 0 | — | — | — | — | — | — | — |
| `transfer_station` | 242 | 222 (91.7%) | 3/7/232 | 4/6/232 | 7/5/230 | 0.0% | 0.0% | 0.0% |
| `composting_facility` | 130 | 125 (96.2%) | 6/0/124 | 5/0/125 | 3/0/127 | 0.0% | 0.0% | 0.0% |
| `anaerobic_digester` | 26 | 25 (96.2%) | 1/0/25 | 1/0/25 | 0/0/26 | 0.0% | 0.0% | 0.0% |

### NC

| Category | n | Geocoded | Septage Y/N/Unk | Grease Y/N/Unk | Porta Y/N/Unk | Phone | Email | Website |
|---|---:|---:|---|---|---|---:|---:|---:|
| `private_regional_septage_facility` | 985 | 516 (52.4%) | 444/0/541 | 186/7/792 | 199/1/785 | 69.2% | 0.0% | 24.5% |
| `potw_receiving_station` | 119 | 3 (2.5%) | 6/0/113 | 6/0/113 | 2/1/116 | 0.0% | 0.0% | 100.0% |
| `land_application_site` | 158 | 1 (0.6%) | 17/0/141 | 7/5/146 | 5/0/153 | 0.0% | 0.0% | 100.0% |
| `transfer_station` | 89 | 89 (100.0%) | 5/1/83 | 0/2/87 | 8/2/79 | 100.0% | 0.0% | 0.0% |
| `composting_facility` | 62 | 61 (98.4%) | 8/0/54 | 7/2/53 | 2/0/60 | 95.2% | 0.0% | 0.0% |
| `anaerobic_digester` | 0 | — | — | — | — | — | — | — |

## 3. Source attribution per category

Each canonical can carry observations from multiple sources. The table below counts how many in-scope canonicals have at least one raw record from each named source. A single canonical can appear in multiple columns when sources cross-reference the same facility (e.g. an ECHO NPDES record collapsing into a CWNS POTW entry).

| State | Category | n | epa_echo | epa_cwns_2022 | tceq_msw_facilities_xls | nc_deq_non_discharge_facilities | nc_deq_solid_waste_facility_list | nc_deq_septage_firm_list | discovery_crawl (pending) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| TX | `private_regional_septage_facility` | 36 | 8 | 0 | 36 | 0 | 0 | 0 | 57 |
| TX | `transfer_station` | 242 | 44 | 1 | 242 | 0 | 0 | 0 | 0 |
| TX | `composting_facility` | 130 | 55 | 0 | 130 | 0 | 0 | 0 | 0 |
| TX | `anaerobic_digester` | 26 | 6 | 0 | 26 | 0 | 0 | 0 | 0 |
| NC | `private_regional_septage_facility` | 985 | 2 | 0 | 0 | 241 | 0 | 744 | 2 |
| NC | `potw_receiving_station` | 119 | 0 | 2 | 0 | 119 | 0 | 0 | 4 |
| NC | `land_application_site` | 158 | 0 | 1 | 0 | 158 | 0 | 0 | 0 |
| NC | `transfer_station` | 89 | 1 | 0 | 0 | 0 | 89 | 0 | 0 |
| NC | `composting_facility` | 62 | 2 | 1 | 0 | 0 | 62 | 1 | 0 |

`discovery_crawl (pending)` are facilities surfaced by the Phase 4.5 discovery crawl that are held out of `v_all_in_scope` until Ryan approves them via the review queue. They are NOT counted in the earlier per-category totals; this column is informational only.

## 4. Acceptance-flag commitment rates (Phase 4 v1.1.1)

Phase 4 enrichment ran Anthropic Haiku 4.5 over the 1,970-row in-scope set under the v1.1.1 prompt (calibration: 92.3% / 85.7% / 90.0% precision on septage / grease / portable). Calibration also captured recall: 70.6% / 75.0% / 90.0% — i.e. `Yes`-committed counts are floor estimates of the underlying truth.

| Field | Yes | No | Unknown | Commit rate |
|---|---:|---:|---:|---:|
| `accepts_septage` | 509 | 9 | 1,452 | 26.3% |
| `accepts_grease_trap` | 233 | 25 | 1,712 | 13.1% |
| `accepts_portable_toilet` | 236 | 9 | 1,725 | 12.4% |

## 5. Geocoding coverage

| State | In-scope | Geocoded | % |
|---|---:|---:|---:|
| NC | 1,413 | 670 | 47.4% |
| TX | 434 | 408 | 94.0% |
| (NULL) | 123 | 93 | 75.6% |

## 6. Known scope limitations

See `docs/v1_scope_limitations.md` for the full record. Summary of what is NOT in the v1 dataset and why:

- **TX private/regional septage**: TCEQ Sludge Transporter registry is robots-disallowed (CRPUB host). 36 canonicals loaded from incidental sources; the Phase 4.5 discovery crawl surfaced 72 additional candidates pending review.
- **TX land application sites**: TCEQ Sludge and Biosolids registry is robots-disallowed. 0 typed via state-loader path. Phase 4.5 discovery surfaced 19 candidates (TCEQ TLAP applications page, news sources) pending review.
- **NC anaerobic digesters**: edocs.deq.nc.gov host is network-gated (TCP-level WAF block). 0 typed via NC DEQ Solid Waste fallback. Phase 4.5 discovery surfaced 61 candidates (NC DEQ press releases, agmrc.org PDFs, RNG news) pending review.
- **County manhole programs**: 0 typed via any state registry (these are county/municipal pages, not state-loader-reachable). Phase 4.5 discovery surfaced 36 candidates pending review.

Texas Public Information Act and manual edocs fallback paths are documented in `docs/v1_scope_limitations.md` §3 (TPIA) and §4 (NC manual drop).
