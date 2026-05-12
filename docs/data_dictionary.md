# Data Dictionary

Per-field reference for the two tables a downstream consumer actually
joins against: **`canonical_facility`** (the consumer-facing entity row)
and **`field_provenance`** (the audit row that explains where every
value came from).

Companion docs:

- `docs/schema.md` — full schema reference, all 15 tables + 20 views
- `docs/sources.md` — per-source pull documentation
- `docs/access_layer.md` — how to query views + run the queries

Conventions used throughout:

- **All timestamps are UTC ISO 8601.** Stored as `TIMESTAMPTZ` in
  Postgres; the loaders and resolver enforce UTC at the application
  layer. Naive datetimes are forbidden.
- **NULL means "we don't know"**, not "we know it's blank." If a
  loader doesn't observe a value, the field stays NULL. We never stub
  coordinates, facility types, or acceptance flags.
- **First non-null wins.** When multiple sources contribute to the
  same canonical row, the resolver processes them in a locked order
  (CWNS → ECHO → TCEQ → NC ND → NC SW → NC SF) and keeps the
  first-seen non-null value. Later raws fill remaining NULLs only.

---

## 1. `canonical_facility` — every field

The 22-column consumer row. Matches `exports/facilities_primary.csv`
and every one of the 20 SQL Views. UUID-keyed.

### Identity

| Field | Type | Allowed values | Source(s) | Transformation |
|---|---|---|---|---|
| `id` | `UUID` | RFC 4122 v4 | resolver `_canonicalize.new_canonical_id()` | Minted in Python (`uuid.uuid4()`) at canonical creation. **Stable across runs** as long as `--rebuild` isn't used; `--rebuild` produces fresh UUIDs. |
| `name` | `TEXT` | Any non-empty string. NULL allowed. | Source-specific (see §5) | Whitespace-trimmed + collapse to single spaces. Case preserved (we don't title-case source-given names). |
| `facility_type` | `TEXT` | One of 7 slugs from `config/facility_types.yaml` (see §3) OR NULL | Computed in `resolver/_category_map.py` | YAML regex match (high confidence) → YAML synonym match (high) → per-source classifier-code override (medium) → NULL with raw type preserved at `field_provenance.field_name='facility_type'`. |

### Address

| Field | Type | Allowed values | Source(s) | Transformation |
|---|---|---|---|---|
| `street` | `TEXT` | Any non-empty string. NULL allowed. | Source-specific | Whitespace-trimmed. Case preserved. |
| `city` | `TEXT` | Any non-empty string. NULL allowed. | Source-specific | Whitespace-trimmed. Case preserved. |
| `state` | `TEXT` | USPS 2-letter, regex `^[A-Z]{2}$`, OR NULL. CHECK enforced. | Source-specific | Trimmed and **upper-cased**. Sources providing a non-2-letter string are rejected → NULL. |
| `zip` | `TEXT` | ZIP-5 or ZIP+4. NULL allowed. No regex CHECK in schema. | Source-specific | Whitespace removed; trailing dash stripped. ZIP+4 preserved when source supplies it. |
| `county` | `TEXT` | Any non-empty string. NULL allowed. | Source-specific | Whitespace-trimmed. Case preserved. |

### Coordinates

The schema enforces global lat/long bounds (`-90..90`, `-180..180`)
via CHECK constraints. The **per-state envelope** check is
application-layer in `orchestration/geocoder.py:coords_consistent_with_state()`
— it downgrades geocoder confidence to `'low'` and sets a review flag
when coords fall outside the state's `STATE_BOUNDS` rectangle.

| Field | Type | Allowed values | Source(s) | Transformation |
|---|---|---|---|---|
| `latitude` | `DOUBLE PRECISION` | -90.0 ≤ x ≤ 90.0 OR NULL | Native coords from source OR Census Geocoder backfill OR NULL on geocoder failure | If source coord is `0.0`, treated as a sentinel → NULL (ECHO uses 0/0 for "no geocoder result"; some state sources do too). The Census Geocoder backfill only enriches when source coords were missing AND `geocoding_cache.confidence='high'` (state-mismatch `'low'` rows are excluded). |
| `longitude` | `DOUBLE PRECISION` | -180.0 ≤ x ≤ 180.0 OR NULL | Same as `latitude` | Same transformation rules. |

### Acceptance flags (tri-state, Phase 4 enrichment)

| Field | Type | Allowed values | Source(s) | Transformation |
|---|---|---|---|---|
| `accepts_septage` | `TEXT` | `Yes` \| `No` \| `Unknown` OR NULL. CHECK enforced. | Phase 4 Haiku enrichment (LLM); manual override; discovery-extract | Written only when an **explicit acceptance signal** is found in source text. No inference from facility type alone. NULL until Phase 4 runs; mostly empty in current v1 state. |
| `accepts_grease_trap` | `TEXT` | Same tri-state | Same | Same |
| `accepts_portable_toilet` | `TEXT` | Same tri-state | Same | Same |

### Contact and pricing

| Field | Type | Allowed values | Source(s) | Transformation |
|---|---|---|---|---|
| `pricing_notes` | `TEXT` | Free-form. NULL allowed. | Phase 4 LLM extraction or manual edit | First-non-null-wins; not currently populated. |
| `phone` | `TEXT` | Free-form (US format). NULL allowed. | Source-specific (NC SW, NC SF provide phone; ECHO/CWNS rarely) | Whitespace-trimmed. Source formats preserved (no `(xxx) xxx-xxxx` normalization). |
| `email` | `CITEXT` | Any email-like string. Case-insensitive comparisons via the `CITEXT` extension. NULL allowed. | Phase 4 discovery extraction; manual edit | Lower-cased at comparison time; storage preserves case. Not currently populated. |
| `website` | `TEXT` | Any URL. NULL allowed. | NC ND `URL` column (rare); Phase 4 discovery | Stored as-given. |

### Cross-source identifiers

These are the IDs that drive the resolver's **ID-first match
precedence** (locked decision 8.10). When a raw row carries any of
them, the resolver matches into an existing canonical via that ID
before falling back to RapidFuzz scoring.

| Field | Type | Allowed values | Source(s) | Transformation |
|---|---|---|---|---|
| `frs_id` | `TEXT` | EPA FRS Registry ID, 12 digits. NULL allowed. | EPA ECHO (`RegistryID`) | Whitespace-trimmed. Acts as a within-ECHO entity-collapse key (~11K within-ECHO matches in current state). |
| `npdes_id` | `TEXT` | NPDES permit, typically `[A-Z]{2}\d{7}` (e.g. `TX0047589`). NULL allowed. | EPA ECHO (`SourceID`), EPA CWNS (`FACILITY_PERMIT[*]` where `PERMIT_SOURCE='NPDES'`) | Whitespace-trimmed. Cross-source: 89 ECHO ↔ CWNS NPDES matches in current state. CWNS NPDES coverage is sparse (~100 of 3,132 CWNS rows). |
| `state_permit_id` | `TEXT` | Source-dependent. NULL allowed. Indexed partial. | First populated of: TCEQ `Additional ID`, NC ND `PERMITNUMBER` (`WQ\d{7}`), NC SW `Facility Id` (`<county>-<type>-<year>`), NC SF `Permit` (`NCS-\d{5}`). | Resolver picks the most-specific available via `resolver/_canonicalize.derive_state_permit_id()`. The actual format is namespace-distinct enough that two sources won't collide on the same string. |

### Timestamps

| Field | Type | Allowed values | Source(s) | Transformation |
|---|---|---|---|---|
| `first_seen_at` | `TIMESTAMPTZ` | UTC ISO 8601. NOT NULL. Defaults to `NOW()` at INSERT. | Resolver writes at canonical creation | Never updated after insert. |
| `last_seen_at` | `TIMESTAMPTZ` | UTC ISO 8601. NOT NULL. Defaults to `NOW()` at INSERT. | Resolver writes at canonical creation; `ON CONFLICT DO UPDATE` sets `NOW()` on every merge | Tracks "when did we last see any raw merge into this canonical." Useful for staleness queries. |

---

## 2. `field_provenance` — every field

One row per `(canonical_facility_id, field_name, observed_at)`. The
audit chain. `canonical_facility` holds the **winning** value; this
table holds the source-attested value for every field at the time the
resolver linked the raw.

| Field | Type | Allowed values | Notes |
|---|---|---|---|
| `id` | `BIGSERIAL` | Auto-increment integer | Primary key. |
| `canonical_facility_id` | `UUID` | FK → `canonical_facility(id)` ON DELETE CASCADE | **Join key.** `field_provenance.canonical_facility_id = canonical_facility.id`. Every provenance row points to exactly one canonical; one canonical has 0..N provenance rows. |
| `field_name` | `TEXT` | Any column name on `canonical_facility` (`name`, `street`, `city`, …, `facility_type`, `state_permit_id`). Unconstrained at the DB level. | Identifies which canonical column this provenance row attests to. |
| `value` | `TEXT` | Free-form text representation. NULL allowed. | Numeric / JSON fields stringify here. Use `(value::numeric)` casts at query time when you need numerics. |
| `source_url` | `TEXT` | URL string. NULL allowed. | Where to look on the source side. Phase 3 resolver writes NULL today; Phase 4 enrichment fills these in. |
| `source_date` | `TIMESTAMPTZ` | UTC ISO 8601. NULL allowed. | **UTC convention enforced**: every source_date written must be tz-aware UTC. The loaders compute this from `source_signature.last_modified` when present. |
| `extraction_method` | `TEXT` | CHECK ∈ {`direct_scrape`, `llm_extracted`, `manual`}. NOT NULL. | `direct_scrape`: the value came from a structured source field (everything Phase 3 writes is this). `llm_extracted`: Phase 4 Haiku produced it from unstructured text. `manual`: human-curated override. |
| `confidence` | `TEXT` | CHECK ∈ {`high`, `medium`, `low`}. NOT NULL. | `high`: source-attested + matches expected shape (Phase 3 default). `medium`: source-attested but borderline (e.g., per-source classifier override produced the facility_type rather than the YAML controlled vocabulary). `low`: shape mismatch or weak inference (Phase 4 review path). |
| `observed_at` | `TIMESTAMPTZ` | UTC ISO 8601. NOT NULL. Defaults to `NOW()`. | When the provenance row was written. Always populated; secondary sort key after `field_name`. |

### Joinability contract

```sql
-- Every populated field on a canonical, with provenance:
SELECT cf.id, cf.name, fp.field_name, fp.value,
       fp.extraction_method, fp.confidence, fp.source_date
  FROM canonical_facility cf
  JOIN field_provenance  fp ON fp.canonical_facility_id = cf.id
 ORDER BY cf.id, fp.field_name, fp.observed_at DESC;
```

A canonical facility with N linked raw observations typically has
N × (number of populated fields) provenance rows. Current state:
72,744 canonicals → 1,020,283 provenance rows (~14 per linked raw).
The 2,363 typed canonicals carry 23,292 provenance rows in the CSV
export's `facilities_provenance.csv` slice.

### `source_date` UTC convention — why it matters

Drift detection (`orchestration/drift_detector.py`) doesn't read
`source_date` directly, but downstream "data freshness" surfaces will
sort by it. A naive (tz-unaware) `source_date` would compare wrong
across DST transitions and would silently misorder rows from sources
in different time zones (TCEQ publishes in US/Central; NC DEQ
publishes in US/Eastern; EPA publishes in UTC). The loaders convert
to UTC before write; the resolver respects whatever the loaders gave
it.

---

## 3. The seven `facility_type` slugs — definitions

The controlled vocabulary lives in `config/facility_types.yaml`
(v1 scope locked). Every canonical facility either carries one of
these slugs or has `facility_type=NULL` (out-of-scope; ~70K rows in
current state, mostly ECHO industrial NPDES).

### `potw_receiving_station`

**POTW Receiving Station** — a Publicly Owned Treatment Works that
operates a dedicated hauler/septage receiving station, typically a
manifested-load offload point connected to headworks.

**Critical scope note (decision A3.3, kickoff-brief Checkpoint 2).**
Synonyms are **receiving-station-specific by design**. Bare
facility-type strings like `WWTP`, `POTW`, `wastewater treatment
plant`, or `treatment works` are deliberately **excluded** from the
synonym list. The reason: only the subset of POTWs that operate a
manifested-load hauler receiving station belong to this category.
Treating `WWTP` alone as a synonym would over-match every wastewater
plant and inflate category 1 with non-receiving facilities.

A bare POTW gets promoted to this canonical type only when an
**explicit acceptance signal** is found — `accepts_septage='Yes'`, a
hauler-manifest reference in source text, a "Septage Receiving
Station" page header, etc. The acceptance-flag enrichment in Phase 4
handles the promotion. Until then, bare POTWs sit under
`facility_type=NULL` in `v_*_in_scope` with their raw type string
preserved at `field_provenance.field_name='facility_type_raw'`.

**Do not loosen the synonyms.** This is the most-frequently-misread
boundary in the controlled vocabulary.

### `county_manhole_program`

**County Manhole Program** — a county or municipal program that
authorizes haulers to discharge into designated sanitary-sewer
manholes (sometimes called "discharge manholes" or "manhole disposal
points"). Not a treatment plant. A permitted discharge point upstream
of one.

Synonyms: `county manhole program`, `manhole disposal program`,
`designated discharge manhole`, `permitted manhole discharge`,
`sanitary sewer manhole disposal`, `hauler manhole discharge program`,
`municipal manhole disposal program`, `septage manhole discharge`.

**Currently 0 rows.** No v1 source publishes this category at the
state level — discovered piecemeal at the city/county level. Phase
4.5 discovery crawl is the v1 path.

### `land_application_site`

**Land Application Site** — a permitted site that receives biosolids
or septage for surface application or sub-surface injection onto
cropland, forestland, pasture, or reclamation land. Includes
registered Class A and Class B biosolids sites as well as septage
land application under 40 CFR 503.

Synonyms include `biosolids land application site`, `septage land
application site`, `Class A/B biosolids land application`, `surface
application site`, `sub-surface injection site`, `biosolids spreader
site`, `sludge land application`.

Distinct from: composting (transforms, doesn't disperse), anaerobic
digester (energy recovery), transfer station (moves, doesn't apply
to land), POTW receiving station (treats wastewater, not the
end-stage residual application).

### `private_regional_septage_facility`

**Private / Regional Septage Facility** — privately or regionally
operated facility that accepts septage, grease trap waste, or
portable toilet waste from haulers for treatment, dewatering, or
transfer.

Distinct from a POTW receiving station (publicly owned) and from a
transfer station (moves waste rather than treating it). The "private
or regional" qualifier matters — single-municipality public WWTPs
are POTWs; multi-jurisdiction private operations are this category.

Synonyms include `commercial septage receiving facility`, `private
hauler waste facility`, `septage treatment facility (private)`,
`grease trap waste facility`, `FOG receiving facility`, `portable
toilet waste facility`, `regional biosolids handling facility`.

### `composting_facility`

**Composting Facility** — a permitted facility that composts
biosolids, food waste, yard waste, or mixed organics. Includes Type
1-5 compost facilities under NC DWM classifications and equivalent
TCEQ permits.

Excludes: pure transfer stations (no composting on site), pure
anaerobic digesters (different end-product).

### `anaerobic_digester`

**Anaerobic Digester** — a facility operating an anaerobic digester
for biosolids, food waste, manure, or co-digestion. Includes
standalone digesters and POTW digesters that are inventoried as
separate units. Includes biogas / renewable natural gas (RNG)
production facilities.

Excludes: pure composting facilities (aerobic) and pure transfer
stations.

### `transfer_station`

**Transfer Station** — a permitted solid-waste or septage transfer
station; a facility where collected waste is consolidated for onward
transport rather than treated on-site. Includes TCEQ Type V transfer
stations and NC DWM transfer-station permits.

**HHW Collection exclusion (filter rule pinned in `build_log.md` →
Phase 3 prep).** NC DEQ DWM `Activity='Collection'` rows are HHW
citizen drop-off points (paint, batteries, electronics) — not
manifested-load hauler transfer stations. They are **excluded by
default** from this category by the resolver. The exception: if a
row's `raw_payload` shows explicit hauler-receiving capability, the
resolver may include it with `confidence='low'` for review. None of
the 30 NC SW Collection rows currently meet that criterion.

The `not_synonyms` list under this slug in `config/facility_types.yaml`
also excludes drinking-water-transfer terms (`water transfer
station`, `drinking water`, `raw water transfer`, `treated water
transfer`, `potable water`) so the broad `\btransfer\s*station\b`
regex doesn't over-match drinking-water infrastructure.

---

## 4. Source slugs — all 16 seeded

The `source` table currently carries **16 slugs**, of which 6 are
**actively loaded** (federal + state loaders that wrote to
`raw_facility_record` during Phase 1–2). The other 10 are placeholder
or umbrella rows that exist for hierarchical organization, future
expansion, or declined-per-locked-decision sources.

Live SELECT from `source` ordered by slug:

| Slug | Type | Status | One-line description |
|---|---|---|---|
| `county_health_placeholder` | `county` | Placeholder | Per-county health-department onsite-wastewater / hauler programs. Concrete rows added per (state, county) as discovery surfaces them. |
| `discovery_crawl` | `discovery_crawl` | Phase 4.5 | Internal source category for the bounded discovery crawl (Brave Search + Haiku extraction). Net-new entities gated by `discovery_review_queue`. |
| **`epa_cwns_2022`** | `federal` | **Loaded** (3,132 rows) | EPA Clean Watersheds Needs Survey 2022. Pulled via Playwright from the APEX app at `sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub`. Per-state ZIP of CSV tables keyed by `CWNS_ID`. |
| **`epa_echo`** | `federal` | **Loaded** (92,326 rows) | EPA Enforcement and Compliance History Online — CWA NPDES facilities. Pulled via the REST `cwa_rest_services.get_facilities` + `.get_download` flow (NOT the robots-disallowed search-results pages). |
| `nc_deq_dwm` | `state` | Umbrella | NC DEQ Division of Waste Management — umbrella row that the three NC DWM subsources (`nc_deq_septage_firm_list`, `nc_deq_solid_waste_facility_list`) hang off. |
| `nc_deq_dwr` | `state` | Umbrella | NC DEQ Division of Water Resources — umbrella row that `nc_deq_non_discharge_facilities` hangs off. |
| **`nc_deq_non_discharge_facilities`** | `state` | **Loaded** (1,259 rows) | NC DEQ DWR Non-Discharge Permits view via ArcGIS REST FeatureServer (`services2.arcgis.com/kCu40SDxsCGcuUWO`). Geometry stripped by NC DEQ for privacy; county-attributed instead. |
| **`nc_deq_septage_firm_list`** | `state` | **Loaded** (759 rows) | NC DEQ DWM Septage Firm Registry. XLSX from edocs.deq.nc.gov; Playwright blocked by network-layer WAF, manual-drop path used. |
| **`nc_deq_solid_waste_facility_list`** | `state` | **Loaded** (435 rows) | NC DEQ DWM Active Solid Waste Facilities. Same edocs.deq.nc.gov manual-drop path. |
| `operator_sites_placeholder` | `operator_site` | Placeholder | Individual operator websites (Synagro, Denali, regional haulers). Concrete rows added per operator with their own ToS audit. |
| `state_npdes` | `registry` | Placeholder | Future expansion-state NPDES interfaces (NY SPDES, CA SWRCB, etc.). v1 TX/NC NPDES handled via TCEQ + NC DEQ DWR. |
| `state_registries_placeholder` | `registry` | Placeholder | State-specific registries outside DEQ/TCEQ (e.g. agriculture department biosolids registries). |
| `tceq_central_registry` | `state` | **Declined** | TCEQ Central Registry (CRPUB). robots.txt = `Disallow: /` — declined per locked decision 8.12. Data accessed via `tceq_public_data_lookup` downloads instead. |
| `tceq_domestic_wastewater` | `state` | Not loaded | TCEQ Domestic Wastewater Permits program landing page. Used for process docs; facility lists reached via `tceq_public_data_lookup`. |
| **`tceq_msw_facilities_xls`** | `state` | **Loaded** (1,494 rows) | TCEQ MSW Active Facilities, weekly-refreshed XLS at a static URL. Legacy BIFF format (xlrd engine). |
| `tceq_public_data_lookup` | `state` | Umbrella | TCEQ Public Data Lookup — supported access path that replaces direct scraping of CRPUB. `tceq_msw_facilities_xls` is a child of this. |

**Active loaders writing to `raw_facility_record`: 6** (the bold rows
above). Cumulative `raw_facility_record` count: **99,405** across the
six active loaders.

---

## 5. `facility_type` → source mapping

Which loaders feed which canonical category. Estimates are post-
Phase-3-dedupe (commit `f9647c9`). See `docs/sources.md` for the
per-source detail.

| Canonical type | Source slug(s) | Estimated rows | How the mapping fires |
|---|---|---:|---|
| `potw_receiving_station` | `nc_deq_non_discharge_facilities` (NC); future: `epa_cwns_2022` + `epa_echo` post-Phase-4 acceptance promotion (cross-state) | 124 | NC ND: `PERMIT_TYPE` substring `'reclaimed water'` (`resolver/_category_map.py`). Cross-state: bare POTWs are NULL-type today; Phase 4 Haiku promotes when explicit acceptance signal found. |
| `county_manhole_program` | `discovery_crawl` (Phase 4.5) | 0 | No v1 source publishes at state level. Discovery is the path. |
| `land_application_site` | `nc_deq_non_discharge_facilities` (NC) | 158 | `PERMIT_TYPE` substring `'residual solids'` covers 503-regulated and 503-Exempt rows. TX side is a known v1 gap — TCEQ registry is in CRPUB (declined). |
| `private_regional_septage_facility` | `nc_deq_septage_firm_list` (NC), `tceq_msw_facilities_xls` (TX), `nc_deq_non_discharge_facilities` (borderline) | 1,420 | NC SF: `Activity='Hauler'` (100% of the 759 rows). TCEQ: `Physical Type ∈ {5GG, 5TL, 5GM}` per GI-613. NC ND: `PERMIT_TYPE` substring `'wastewater irrigation'` (borderline — see Phase 4 SFR filter pin in `build_log.md`). |
| `composting_facility` | `tceq_msw_facilities_xls` (TX), `nc_deq_solid_waste_facility_list` (NC) | 220 | TCEQ: `Physical Type ∈ {5RC, 5RCX}`. NC SW: `Activity='Compost'`. |
| `anaerobic_digester` | `tceq_msw_facilities_xls` (TX) | 34 | `Physical Type='9GR'` (Beneficial Gas Recovery). NC side is a known v1 gap — no NC-specific public roster surfaced in audit. |
| `transfer_station` | `tceq_msw_facilities_xls` (TX), `nc_deq_solid_waste_facility_list` (NC) | 413 | TCEQ: `Physical Type ∈ {5TS, 5LV, 5CC}` (5CC is the citizens-collection-station NOI tier, included per GI-613). NC SW: `Activity='Trans'`. The 30 NC SW `Activity='Collection'` rows are excluded by the HHW filter rule. |

Aggregate v1-typed canonicals: **2,363**. The remaining **70,381**
`canonical_facility` rows have `facility_type=NULL` — mostly ECHO
industrial NPDES outside the 7-category v1 scope, filtered at CSV
export.

For per-source pull mechanics, refresh cadence, ToS posture, and
audit notes: see `docs/sources.md`.
