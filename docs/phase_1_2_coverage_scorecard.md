# Phase 1 + 2 Coverage Scorecard

Snapshot taken 2026-05-12, immediately after Phase 2 step 4
(`nc_deq_septage_firm_list`) committed and verified. This is the
canonical pre-Phase-3 inventory: it counts what landed in
`raw_facility_record`, compares against the original audit
predictions, and surfaces the likely v1 category yield per source
once the resolver's filter rules are applied.

All counts are pulled live from Supabase
(`SELECT s.slug, COUNT(*) FROM raw_facility_record r JOIN source s
ON s.id = r.source_id GROUP BY s.slug`) — no estimates, no caches.
Distribution columns are pulled from `raw_payload` JSONB and listed
verbatim from the source schema.

---

## 1. Per-source row counts (raw_facility_record)

| Source slug | Rows | TX | NC | Other | Predicted | Status |
|---|---:|---:|---:|---:|---|---|
| `epa_echo` | 92,326 | 72,493 | 19,822 | 11 | "cross-state CWA universe, ~80–100k" | **In range.** Other-state stragglers (OK 3, VA 2, LA 2, SC 2, MD 1, AR 1) survive raw load and are filtered at canonical resolution via the `CWPState ∈ {TX,NC}` rule. |
| `epa_cwns_2022` | 3,132 | 2,312 | 820 | 0 | "2,312 TX + 820 NC per APEX 2022 export" | **Exact match.** |
| `tceq_msw_facilities_xls` | 1,494 | 1,494 | 0 | 0 | "~1,500 active MSW facilities per GI-613" | **In range.** 256 rows have `Physical Site Status='NOT CONSTRUCTED'` (filtered at canonical resolution); 1,184 ACTIVE + 54 INACTIVE remain. |
| `nc_deq_non_discharge_facilities` | 1,259 | 0 | 1,259 | 0 | "~1,200–1,500 NC non-discharge permits per ArcGIS layer" | **In range.** Geometry stripped by NC DEQ (privacy decision — 47% are single-family residences); county-attribution preserved. |
| `nc_deq_septage_firm_list` | 759 | 0 | 758 | 1 | "~750 NC-registered septage haulers per DWM roster" | **In range.** 1 out-of-state firm (NCS-01837 in Conway, SC) kept as NC-regulatory scope; flagged for low-confidence canonicalization. |
| `nc_deq_solid_waste_facility_list` | 435 | 0 | 435 | 0 | "~400–500 active NC SW facilities per DWM list" | **In range.** 30 `Activity='Collection'` rows are HHW citizen drop-offs — excluded by default at canonical resolution (see filter rules below). |
| **TOTAL** | **99,405** | **76,299** | **23,134** | **12** | — | **6 of 6 sources within audit range.** |

**State split rationale.** Three sources have no `State` column in
the source data: `nc_deq_non_discharge_facilities` (NC by ArcGIS
construction), `nc_deq_solid_waste_facility_list` (verified 435/435
rows have `State='NC'` in the XLSX), and `nc_deq_septage_firm_list`
(no state column; 758 of 759 rows have a NC `County`, 1 has
`County='-'` and a SC physical address). The "NC" column for
those sources is by-construction inference, not per-row assertion.

---

## 2. v1 category-target yield (post-filter, pre-resolver)

Yield estimates apply the filter rules pinned in
`build_log.md → Phase 3 prep` (CWPState filter, NOT CONSTRUCTED
filter, HHW Collection exclusion). All counts are upper-bound
estimates from source-level classifiers (`Physical Type`,
`Activity`, `PERMIT_TYPE`, `FACILITY_TYPE`, `CWPSICCodes`); the
resolver may downgrade individual rows during canonicalization on
the basis of name/address evidence.

### Category 1 — POTW (Public Owned Treatment Works)

| Source | Estimated rows | Basis | Notes |
|---|---:|---|---|
| `epa_cwns_2022` | ~2,200 (TX) + ~750 (NC) ≈ 2,950 | `FACILITY_TYPES[*].FACILITY_TYPE='Treatment Plant'` (2,207 tags total) + Collection tags | CWNS is the **primary cat-1 spine**. Some rows are non-POTW WW collection systems — Phase 3 resolver narrows. |
| `epa_echo` | ~4,500–5,000 (TX) + ~1,000 (NC) ≈ 5,500 | `CWPSICCodes='4952'` (Sewerage Systems) = 4,958 across both states | Cross-reference against CWNS via NPDES `SourceID` ↔ CWNS `FACILITY_PERMIT.NPDES_PERMIT_NUMBER`. Most cat-1 ECHO rows overlap with CWNS rows; the resolver merges them via ID-first matching. |
| **Estimated unique cat 1 (post-merge)** | **~3,000–3,500** | CWNS + ECHO merged on NPDES | The non-overlap residual (ECHO-only or CWNS-only) is small. CWNS dominates because EPA designed CWNS to be the POTW survey. |

### Category 2 — Industrial Wastewater Treatment / Discharge

| Source | Estimated rows | Basis | Notes |
|---|---:|---|---|
| `epa_echo` | ~30,000–40,000 (TX) + ~8,000 (NC) | Non-`4952` NPDES permits with industrial SIC codes (3000-series manufacturing, 4900-series utilities ex-sewerage, etc.) | The ECHO universe is mostly industrial / commercial NPDES dischargers. Cat 2 is the **largest single v1 category** by raw count, sourced almost entirely from ECHO. |
| `nc_deq_non_discharge_facilities` | ~250 (NC) | `PERMIT_TYPE='Wastewater Irrigation'` (215) + `'Closed-Loop Recycle'` (36) | Non-discharge industrial WW. NC-only — TX equivalent is in CRPUB (declined per locked decision 8.12). |
| **Estimated unique cat 2** | **~38,000–45,000** | — | Coarse — Phase 3 narrows via SIC code allowlist. The cat-2 boundary is the squishiest of the seven categories. |

### Category 3 — Biosolids / Residuals Land Application

| Source | Estimated rows | Basis | Notes |
|---|---:|---|---|
| `nc_deq_non_discharge_facilities` | 195 (NC) | `PERMIT_TYPE LIKE '%Residual Solids%'` — 96 Land App 503 + 37 Distribution 503 + 4 Surface Disposal 503 + 16 Land App 503-Exempt + 36 Distribution 503-Exempt + 6 Surface Disposal 503-Exempt | **Direct cat-3 hit, NC side.** The "503" tags map to EPA 40 CFR Part 503 biosolids rules. NC ND is the v1 cat-3 spine for NC. |
| `epa_cwns_2022` | 25 (cross-state) | `FACILITY_TYPES[*].FACILITY_TYPE='Biosolids Handling Facility'` | Small but explicit. Cross-references CWNS POTW spine to flag which POTWs have biosolids handling. |
| TX side | **0 direct** | — | TX biosolids land-application registry is in CRPUB (declined). Cat 3 TX coverage in v1 is **gap-known**, documented in `v1_scope_limitations.md`. |
| **Estimated cat 3** | **~220** | NC + the federal cross-flag | Strong on NC; thin on TX. |

### Category 4 — Private / Regional Septage

| Source | Estimated rows | Basis | Notes |
|---|---:|---|---|
| `nc_deq_septage_firm_list` | 759 (NC + 1 SC) | 100% of rows (`Activity='Hauler'` uniform) | **Direct cat-4 hit, NC side.** Pure hauler-firm roster — every row is a registered septage business. |
| `tceq_msw_facilities_xls` | ~80 (TX, upper bound) | `Physical Type ∈ {5GG, 5TL, 5GM}` — Liquid Waste Processing / Transfer / Mobile | Partial. `5GG` (44 rows) overlaps grease-trap and septage handling — Phase 3 resolver flags ambiguity. `5TL` and `5GM` ranges expected from GI-613 but counts in current load are small. |
| `nc_deq_non_discharge_facilities` | 589 (NC, borderline) | `PERMIT_TYPE='Single-Family Residence Wastewater Irrigation'` | **Borderline** — these are residential decentralized wastewater systems (septic-like), not regulated hauler firms. Phase 3 may exclude these from cat 4 strictly, or include them with a "decentralized" subtype. Decision pending. |
| **Estimated cat 4 (strict)** | **~840** | NC SF + TCEQ liquid-waste subset | Decentralized-systems handling deferred to Phase 3 decision. |

### Category 5 — Composting

| Source | Estimated rows | Basis | Notes |
|---|---:|---|---|
| `tceq_msw_facilities_xls` | 172 (TX) | `Physical Type ∈ {5RC, 5RCX}` — 76 + 96 | Direct hit per GI-613. `5RCX` rows are NOI-tier (lower confidence than `5RC` permitted). |
| `nc_deq_solid_waste_facility_list` | 63 (NC) | `Activity='Compost'` | Direct hit. NC DEQ DWM list is the cat-5 spine for NC. |
| **Estimated cat 5** | **~235** | TX + NC | |

### Category 6 — Anaerobic Digester / Biogas Recovery

| Source | Estimated rows | Basis | Notes |
|---|---:|---|---|
| `tceq_msw_facilities_xls` | 45 (TX) | `Physical Type='9GR'` — Registered Beneficial Gas Recovery Facility | Direct hit per GI-613. **Only public-list cat-6 contribution in v1.** |
| Cross-state | 0 direct | — | NC DEQ DWM list has no Activity code for digesters as a distinct facility category; AD coverage for NC is **gap-known**. |
| **Estimated cat 6** | **~45** | TX only | Smallest v1 category by raw count; expected. |

### Category 7 — Waste Transfer / Material Recovery

| Source | Estimated rows | Basis | Notes |
|---|---:|---|---|
| `tceq_msw_facilities_xls` | 388 (TX) | `Physical Type ∈ {5TS, 5LV, 5CC}` — 157 + 38 + 193 | `5CC` (Citizens Collection Stations) coded as a cat-7 small/municipal subtype per GI-613 — TCEQ explicitly treats these as transfer stations. |
| `nc_deq_solid_waste_facility_list` | 92 (NC) | `Activity='Trans'` | Direct hit. The 30 additional `Activity='Collection'` rows are HHW citizen drop-offs — **excluded by default** per the Phase 3 prep filter; the resolver may include with `confidence='low'` if a row's `raw_payload` shows explicit hauler-receiving capability. |
| **Estimated cat 7** | **~480** | TX + NC | Strong on both sides. NC has 30 borderline HHW Collection rows held out by default. |

### Summary table (all seven categories)

| Category | Est. rows | Primary source | TX | NC |
|---|---:|---|---|---|
| 1 — POTW | ~3,000–3,500 | `epa_cwns_2022` + `epa_echo` (NPDES merge) | strong | strong |
| 2 — Industrial WW | ~38,000–45,000 | `epa_echo` (industrial NPDES) | strong | strong |
| 3 — Biosolids | ~220 | `nc_deq_non_discharge_facilities` (503 permits) | **gap** | strong |
| 4 — Private septage | ~840 (strict) | `nc_deq_septage_firm_list` + TCEQ liquid-waste subset | thin | strong |
| 5 — Composting | ~235 | TCEQ 5RC/5RCX + NC SW Compost | strong | medium |
| 6 — Anaerobic digester | ~45 | TCEQ 9GR | thin | **gap** |
| 7 — Transfer station | ~480 | TCEQ 5TS/5LV/5CC + NC SW Trans | strong | strong |
| **TOTAL v1-relevant** | **~42,800–50,300** | (sum of category estimates) | | |

Note that the v1-relevant total is a fraction of the 99,405 raw
rows. The largest single contributor by raw count is `epa_echo`
(92,326 rows), most of which map to cat 2 (industrial NPDES) or
**no v1 category at all** (construction stormwater, land
development, etc.). Phase 3's resolver applies the SIC-code
allowlist + CWPState filter to narrow the ECHO universe.

---

## 3. Filter rules recap (pinned in build_log → Phase 3 prep)

Four rules apply during canonical resolution. Raw rows are **not**
filtered at load time — `raw_facility_record` keeps source-of-record
fidelity. Filters apply at the resolver layer.

| Rule | Source | Action | Reason |
|---|---|---|---|
| **CWPState ∈ {TX, NC}** | `epa_echo` | Skip 11 stragglers (OK 3, VA 2, LA 2, SC 2, MD 1, AR 1) | v1 coverage set is `{TX, NC}`. ECHO's API parameter is permissive on out-of-state related-party rows. |
| **NOT CONSTRUCTED filter** | `tceq_msw_facilities_xls` | Skip 256 rows where `Physical Site Status='NOT CONSTRUCTED'` | Permitted-but-never-built facilities have a permit number but no physical site to canonicalize against. |
| **HHW Collection exclusion** | `nc_deq_solid_waste_facility_list` | Default-exclude 30 rows where `Activity='Collection'`. Exception: include with `confidence='low'` if `raw_payload` shows hauler-receiving capability. | Category 7 means manifested-load transfer for haulers, not citizen-served HHW drop-off. NC's Collection variant lacks TCEQ 5CC's hauler-receiving overlap. |
| **ID-first match override** | all sources | Match on stable identifiers (NPDES `SourceID`, FRS `RegistryID`, CWNS `CWNS_ID`, TCEQ `Additional ID`+`RN`, NC `PERMITNUMBER`, NC `Facility Id`, NC `Permit`) before falling back to RapidFuzz name+city scoring | Locked decision 8.10 from the kickoff brief. |

---

## 4. Open items / known gaps going into Phase 3

These are the **gap-known** items already documented in
`v1_scope_limitations.md` and the Phase 2 step build_log entries.
Listed here for the resolver's awareness:

1. **TX biosolids (cat 3)**: no public list; TCEQ keeps the
   registry in CRPUB (declined per locked decision 8.12). Cat-3
   coverage in v1 is **NC-only**.
2. **NC anaerobic digesters (cat 6)**: no NC-specific public
   roster surfaced in the audit. Cat-6 v1 coverage is **TX-only
   via TCEQ 9GR** (45 rows).
3. **TX-side decentralized septage / OWTS**: equivalent to NC's
   `Single-Family Residence Wastewater Irrigation` (589 NC rows)
   exists in TCEQ's OSSF database, which is in CRPUB (declined).
4. **NC ND geometry stripped**: 1,259 NC rows have NULL geometry
   by NC DEQ's privacy decision. County-attribution preserved;
   the 200m proximity tiebreak rule cannot apply to these rows
   (resolver treats as "no tiebreak available," not "no match").
5. **Borderline cat-4 decision pending**: 589 NC ND
   `Single-Family Residence Wastewater Irrigation` rows — should
   they map to cat 4 strict, cat 4 with a "decentralized" subtype,
   or be excluded? Phase 3 resolver decides; document the decision
   inline.
6. **EPA ECHO state stragglers**: 11 out-of-state rows
   (OK/VA/LA/SC/MD/AR) survive the raw load. Resolver filters
   them via `CWPState ∈ {TX, NC}`; counted in raw_facility_record
   for audit completeness.
7. **Cat 2 SIC-allowlist**: cat 2's exact boundary depends on
   which SIC codes Phase 3 includes/excludes. The
   `epa_echo` SIC distribution shows that construction-related
   SIC codes (1521, 1542, 1611, 1623, 1629, etc.) account for
   ~30,000+ rows that almost certainly are stormwater
   construction permits, not industrial wastewater treatment.
   Resolver SIC allowlist is a Phase 3 design decision.

---

## 5. Pre-Phase-3 sanity checks (all passing)

- **Cumulative raw_facility_record**: 99,405 across 6 sources.
- **Source seed**: 16 rows in `source` table (per CI assertion).
- **Schema migrations applied**: all 4 migrations in
  `supabase/migrations/` (initial DDL + NC seed + GI-613 capture +
  last_modified column) — CI iterates lexically.
- **Source-signature drift baseline**: every source has a
  `source_signature` row with `schema_hash`, `row_count`,
  `byte_size`, and (where available) `last_modified` — drift
  detector compares against this on next run.
- **Idempotency**: all 6 loaders verified to produce
  `inserted=0, updated=0, unchanged=N` on a same-data re-run.

---

## 6. Recommendation

Phase 1 + 2 raw coverage is **complete and within audit predictions
on all 6 sources**. The resolver inputs are ready: 99,405 raw rows
with stable identifiers documented, filter rules pinned, and gap
items surfaced.

**Greenlight Phase 3 entity resolution (Days 5–6).** Start by
implementing the ID-first match path (locked decision 8.10) using
the identifier table at `build_log.md → Phase 3 prep`, then the
RapidFuzz score-based fallback per the brief (auto-merge ≥ 92,
hold-for-review 75–91, reject < 75). Apply the three row-exclusion
filters at canonical-resolution time. Decisions 1–7 above will need
inline answers during Phase 3; defer to Ryan when the call has
business-rule implications (e.g. decentralized-systems handling for
cat 4).
