# Checkpoint 2 — Self-Verification Report v2

**Date:** 2026-05-11T20:30:00Z (UTC)
**Auditor:** Claude Code (self)
**Scope:** Surgical re-verification of the seven items modified after Ryan's
decisions plus the B2 follow-up. Items that already passed in v1 are not
re-checked.
**Status:** Hold for Ryan's final approval. No commits. No migrations applied.
No push. The regex compile smoke test was re-run after the YAML edit and
confirmed 23/23 patterns still compile.

---

## A1.3 — `state` CHECK forward-compat comment added (regex unchanged)

**Status: PASS**

Decision applied: keep broader `^[A-Z]{2}$`; document rationale in a column
comment.

Quoted evidence from `db/migrations/0001_initial.sql:141-146`:

```sql
    -- state: USPS 2-letter code. Regex deliberately accepts any USPS code, not
    -- only 'TX'/'NC' — the kickoff brief allows $40/state additions after v1
    -- delivery and the schema is forward-compatible without a migration per
    -- added state. (See Checkpoint-2 self-review A1.3.)
    CONSTRAINT canonical_facility_state_chk
        CHECK (state IS NULL OR state ~ '^[A-Z]{2}$'),
```

CHECK regex is unchanged from v1. The comment explains why.

---

## A1.5 — Acceptance-flag CHECKs changed to `('Yes','No','Unknown')`

**Status: PASS**

Decision applied: match brief casing verbatim; CSV export reads stored
values directly.

Quoted evidence from `db/migrations/0001_initial.sql:147-154`:

```sql
    -- Acceptance flag values match kickoff-brief section 7 verbatim:
    -- 'Yes' | 'No' | 'Unknown'. CSV export reads stored values directly.
    CONSTRAINT canonical_facility_accepts_septage_chk
        CHECK (accepts_septage IS NULL OR accepts_septage IN ('Yes','No','Unknown')),
    CONSTRAINT canonical_facility_accepts_grease_chk
        CHECK (accepts_grease_trap IS NULL OR accepts_grease_trap IN ('Yes','No','Unknown')),
    CONSTRAINT canonical_facility_accepts_porta_chk
        CHECK (accepts_portable_toilet IS NULL OR accepts_portable_toilet IN ('Yes','No','Unknown')),
```

The inline column comment on lines 126-127 also reflects the change:

```sql
    -- Acceptance flags. Tri-state: 'Yes' | 'No' | 'Unknown' (matches the
    -- kickoff brief section 7 verbatim; CSV export reads these values directly).
    accepts_septage          TEXT,
    accepts_grease_trap      TEXT,
    accepts_portable_toilet  TEXT,
```

`grep` confirms **exactly three** matches of the new CHECK pattern, no
residual lowercase variants:

```
0001_initial.sql:150  CHECK (accepts_septage IS NULL OR accepts_septage IN ('Yes','No','Unknown')),
0001_initial.sql:152  CHECK (accepts_grease_trap IS NULL OR accepts_grease_trap IN ('Yes','No','Unknown')),
0001_initial.sql:154  CHECK (accepts_portable_toilet IS NULL OR accepts_portable_toilet IN ('Yes','No','Unknown')),
```

---

## A1.6 — Lat/long bounds kept global; geocoder module created with policy docstring

**Status: PASS**

Decision applied: keep global CHECK bounds; enforce state-consistency in the
geocoder module; downgrade confidence and set review flag on mismatch.

### Schema constraint comment

`db/migrations/0001_initial.sql:155-164`:

```sql
    -- lat/long: global bounds by design (forward-compat for future states).
    -- State-consistency is enforced at the *application* layer: the geocoder
    -- module compares resolved coords against the per-state envelope and
    -- downgrades confidence to 'low' on mismatch (sets a review flag).
    -- See orchestration/geocoder.py for the policy + STATE_BOUNDS dict.
    -- (Checkpoint-2 self-review A1.6.)
    CONSTRAINT canonical_facility_lat_chk
        CHECK (latitude IS NULL OR (latitude BETWEEN -90 AND 90)),
    CONSTRAINT canonical_facility_lng_chk
        CHECK (longitude IS NULL OR (longitude BETWEEN -180 AND 180))
```

### Geocoder module

New file at `orchestration/geocoder.py` (4.5 KB). Module-level docstring
documents the full policy contract. Excerpt:

```python
"""
geocoder.py
Arch Legacy Partners — Wastewater Facility Database

Geocoder module: US Census Geocoder integration plus the
**state-consistency warning policy** documented below.

...
Policy contract — every loader and the discovery extractor MUST consume
geocoded coordinates through `geocode_with_state_check()` (or an equivalent
that calls `coords_consistent_with_state()`). The contract is:

  1) Geocode the address via US Census Geocoder. Cache the response in
     `geocoding_cache` (address_hash → lat/lng/confidence/matched_address).
  2) If geocoder returns NO match → return (lat=None, lng=None,
     confidence='failed'). Per locked decision 8.5 we NEVER stub coords.
  3) If geocoder returns a match → check `coords_consistent_with_state()`
     against the facility's `state` field using STATE_BOUNDS below.
  4) If the coords fall *outside* the state envelope:
        - Downgrade the geocoder confidence to 'low'
        - Emit a `state_coord_mismatch` warning at field_provenance level
        - Set a review flag (write to `discovery_review_queue` if the row
          originated from discovery; otherwise log to scraper_run.error_message
          with a non-fatal marker so the resolver flags it for human review)
        - Still write the lat/lng — we don't drop the data; we just lower
          its trust score so the review surface picks it up
  5) If the coords fall *inside* the state envelope but `matched_address`
     differs significantly from the input → drop confidence one tier
     (high → medium, medium → low) per the brief section 8.5.
"""
```

### STATE_BOUNDS envelope dict

```python
STATE_BOUNDS: dict[str, tuple[float, float, float, float]] = {
    # state: (min_lat, max_lat, min_lng, max_lng)
    "TX": (25.5, 36.7, -106.8, -93.4),
    "NC": (33.7, 36.7, -84.5, -75.3),
}
```

### `coords_consistent_with_state` smoke test result

Ran 7 cases (Austin TX, Raleigh NC, mismatched coords, None inputs, unknown
state). **7/7 PASS:**

```
  (30.27,-97.74,  'TX') -> inside   expected inside   OK
  (35.78,-78.64,  'NC') -> inside   expected inside   OK
  (30.27,-97.74,  'NC') -> outside  expected outside  OK
  (40.71,-74.0,   'TX') -> outside  expected outside  OK
  (None, None,    'TX') -> unknown  expected unknown  OK
  (30.27,-97.74,  None) -> unknown  expected unknown  OK
  (30.27,-97.74,  'CA') -> unknown  expected unknown  OK
```

`geocode_with_state_check()` is a Phase-1 stub (raises NotImplementedError) — its
docstring carries the implementation contract for the Phase-1 builder so the
loaders can be written against a stable interface before the Census client
ships.

---

## A2.3 + A2.5 — `tceq_public_data_lookup` added as the 12th source row

**Status: PASS**

Decision applied: add as 12th row, keep `tceq_domestic_wastewater`, update
audit doc count.

Quoted evidence from `db/migrations/0002_source_seed.sql:103-119`:

```sql
(
    'tceq_public_data_lookup',
    'TCEQ Public Data Lookup',
    'state',
    'https://www.tceq.texas.gov/agency/data/lookup-data',
    'https://www.tceq.texas.gov/help/policies/index.html',
    'permissive',
    'allow',
    'Umbrella catalogue of TCEQ public data downloads — the supported access path that '
    'replaces direct scraping of CRPUB (which is robots-disallowed). Day-1 reconnaissance '
    'found six relevant lookup paths including: '
    '"Waste Management Permit Applications, Permits, Registrations, and Facilities" '
    '(landfills, transfer stations, MSW, composting) and "Status of Stormwater and '
    'Wastewater Applications and Specifications" (TPDES / domestic wastewater). '
    'robots.txt on www.tceq.texas.gov allows our paths.',
    NOW()
),
```

Inserted between `tceq_central_registry` (lines 81-102) and
`tceq_domestic_wastewater` (lines 120-134) — semantic order: ban →
catalogue → program landing.

### 12-row order confirmed

Tuple-opener line scan of `0002_source_seed.sql`:

```
line  25  'epa_echo'
line  39  'epa_cwns_2022'
line  64  'state_npdes'
line  82  'tceq_central_registry'
line 104  'tceq_public_data_lookup'        ← NEW
line 121  'tceq_domestic_wastewater'
line 140  'nc_deq_dwr'
line 152  'nc_deq_dwm'
line 170  'county_health_placeholder'
line 185  'state_registries_placeholder'
line 198  'operator_sites_placeholder'
line 216  'discovery_crawl'
```

Count: **12** distinct slugs. SQL structural sanity:
`INSERT INTO source` = 1, `VALUES` = 1, `ON CONFLICT` clause = 1
(plus 1 mention in header comment), `BEGIN; COMMIT;` balanced, parens
balanced (61 open, 61 close).

The audit doc reference of "eleven source rows" was updated to twelve;
see the A2.5 audit-doc evidence under the Audit-Doc section below.

---

## A2.4 — TCEQ `tos_url` decision + explicit no-ToS notes on 5 placeholder/internal rows

**Status: PASS**

### TCEQ ToS investigation result (reported per Ryan's instruction)

- **`https://www.tceq.texas.gov/agency/main_terms.html`** — verified 2026-05-11
  via `requests.get` → **HTTP 404** (page title "TCEQ - Texas Commission on
  Environmental Quality - www.tceq.texas.gov", h1 "404 Resource Not Found").
  This URL does not exist.
- TCEQ home-page footer surfaces two policy links: "Disclaimer" →
  `/help/policies/disclaimer_policy.html` and "Site Policies" →
  `/help/policies/index.html`.
- The "Website Policies" index page exists (HTTP 200, title
  "Website Policies - Texas Commission on Environmental Quality") and is
  the umbrella aggregating four sub-policies: **Privacy Policies**,
  **Website Accessibility Policy**, **Public Domain and TCEQ Linking Policy**,
  and **Site Disclaimer**.
- The Public Domain and Linking Policy (`/help/policies/linking_policy.html`)
  explicitly addresses third-party use of TCEQ content; verbatim sentence:
  *"Unless otherwise noted, content of our site is considered 'public
  domain.' We have no restrictions on linking to our site as long as a fee
  is not charged to access our material."*
- TCEQ **does not** publish a single document titled "Terms of Service" or
  "Terms of Use."

**Chosen URL:** `https://www.tceq.texas.gov/help/policies/index.html`
(Website Policies umbrella). Rationale: it links to all four policy
sub-documents — readers asking "what governs my use of this data?" land
here and can drill in. The Public Domain and Linking Policy alone would
miss the Site Disclaimer.

### Applied to both TCEQ rows

`db/migrations/0002_source_seed.sql:86` (tceq_central_registry):

```sql
    'https://www.tceq.texas.gov/help/policies/index.html',
```

`db/migrations/0002_source_seed.sql:108` (tceq_public_data_lookup):

```sql
    'https://www.tceq.texas.gov/help/policies/index.html',
```

`db/migrations/0002_source_seed.sql:125` (tceq_domestic_wastewater):

```sql
    'https://www.tceq.texas.gov/help/policies/index.html',
```

(All three TCEQ rows updated — same ToS document governs all three.)

The `tceq_central_registry` notes also explain the investigation history
(lines 94-100):

```sql
    'ToS note: TCEQ does not publish a single document titled "Terms of Service"; the '
    'closest equivalent is the Website Policies index '
    '(https://www.tceq.texas.gov/help/policies/index.html), which aggregates the Site '
    'Disclaimer, Public Domain and Linking Policy, Privacy, and Accessibility policies. '
    'The Public Domain and Linking Policy (/help/policies/linking_policy.html) states '
    'TCEQ web content is public domain unless otherwise noted. The earlier candidate URL '
    'main_terms.html returns 404 (verified 2026-05-11).',
```

### Explicit "No ToS" notes on 5 placeholder/internal rows

`grep` confirms **exactly five** placements of the sentence
`'No Terms of Service URL applicable; this is a placeholder or internal source.'`:

```
0002_source_seed.sql:74   state_npdes
0002_source_seed.sql:181  county_health_placeholder
0002_source_seed.sql:194  state_registries_placeholder
0002_source_seed.sql:208  operator_sites_placeholder
0002_source_seed.sql:227  discovery_crawl
```

---

## A3.3 — POTW receiving-station synonym scope header comment added

**Status: PASS**

Decision applied: keep my synonym design; document why bare
facility-type strings are excluded.

Quoted evidence from `config/facility_types.yaml:39-52`:

```yaml
  # ---------------------------------------------------------------------------
  # SYNONYM SCOPE NOTE (Checkpoint-2 decision A3.3):
  # Synonyms here are *receiving-station-specific*. Bare facility-type strings
  # like "WWTP", "POTW", "wastewater treatment plant", or "treatment works"
  # are deliberately EXCLUDED from this synonym list. Reason: only the subset
  # of POTWs that operate a manifested-load hauler receiving station belong
  # to category 1. Treating "WWTP" alone as a synonym would over-match every
  # wastewater plant and inflate category 1 with non-receiving facilities.
  # The acceptance-flag enrichment in Phase 4 promotes a bare POTW to this
  # canonical type only when an acceptance signal (accepts_septage='Yes', a
  # hauler manifest reference, etc.) is found. Unmatched raw types fall back
  # to canonical_type='unknown' with confidence='low' at field_provenance.
  # ---------------------------------------------------------------------------
  potw_receiving_station:
```

Synonym list itself is unchanged.

---

## A3.6 — Drinking-water exclusions added to `transfer_station` deny list

**Status: PASS**

Decision applied: add the proposed five drinking-water exclusions.

Quoted evidence from `config/facility_types.yaml:254-267`:

```yaml
    not_synonyms:
      - "land application"
      - "composting"
      - "anaerobic digester"
      - "POTW"
      - "manhole"
      # Drinking-water exclusions (Checkpoint-2 decision A3.6) — prevent the
      # transfer-station regex from over-matching drinking-water infrastructure
      # that happens to contain the word "transfer" or "water transfer."
      - "water transfer station"
      - "drinking water"
      - "raw water transfer"
      - "treated water transfer"
      - "potable water"
```

All five proposed exclusions added.

### Regex still compiles

Re-ran the smoke test after the YAML edit. **23/23 patterns compile, 0
failures.** Raw result excerpted from `local/_self_review_results.json`:

```json
"regex_smoke_test": {
  "total": 23,
  "failures": []
}
```

---

## B2 — CWNS APEX `download-state-zip` discovery captured

**Status: PASS**

Decision applied: add note to `epa_cwns_2022` source row + the audit doc;
schedule the two-request flow validation as a 30-minute Phase-1 spike.

### In `db/migrations/0002_source_seed.sql:46-55` (epa_cwns_2022 notes)

```sql
    'Public US-gov data. Downloadable Excel and Access database releases. Static dataset; '
    'no on-page scraping required. '
    'NOTE (Checkpoint-2 / B2): the 2022 CWNS Data Dashboard at '
    'sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub exposes a session-scoped '
    '`/download-state-zip?p2_location_id=<STATE>&session=<S>&cs=<CS>` endpoint. '
    'A Phase-1 spike (target: 30 min, executed at the federal data-load step) '
    'will validate the two-request flow (GET /about -> capture session+cs; '
    'GET /download-state-zip). If it works, the CWNS loader uses it and '
    'skips Playwright. If not, fall back to Playwright automation against '
    'the APEX app and document the negative result on the build_log.',
```

### In `docs/source_audit_phase0.md` (new "Checkpoint-2 follow-ups" section)

```markdown
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
1. Issue the anonymous-session GET, extract `session` and `cs` ...
2. Replay the download endpoint with `p2_location_id=TX` ...
3. If a real zip arrives → unzip, sanity check the schema, document the
   flow, and wire the CWNS loader to it (no Playwright needed).
4. If not → record the negative result in `docs/build_log.md`, and fall
   back to Playwright automation against the APEX app.
```

(Full content at `docs/source_audit_phase0.md` lines 336-376, slightly
abbreviated above.)

### Live re-fetch corroboration

Re-ran the B2 web check after edits. The endpoint is still present (with a
fresh session token, as expected — the dashboard issues a new one per
visit):

```
/ords/sfdw_pub/r/sfdw/cwns_pub/download-state-zip
  ?p2_location_id=DD
  &session=469272992048
  &cs=16CohG879kRukk8FzXPo2CpQ-Ez8rWcdORFsaZepMCPL9Rk6_loMyAvIYmSLJUHd-gEClzliHaIuYCTJmScChcA
```

---

## Audit doc source count: 11 → 12

**Status: PASS**

Quoted evidence from `docs/source_audit_phase0.md` (within the new
"Checkpoint-2 follow-ups" section):

```markdown
### Source count

This audit originally referenced eleven source rows in
`db/migrations/0002_source_seed.sql`. Following Checkpoint-2 decision
A2.3 + A2.5, **a twelfth row `tceq_public_data_lookup` was added** to
explicitly represent the TCEQ Public Data Lookup as a distinct source
(allow-listed via robots.txt), separate from `tceq_central_registry`
(disallow-listed) and `tceq_domestic_wastewater` (program landing).
Wherever this document says "11 sources" it should be read as 12.
```

---

## Summary

| Check ID | v1 status | v2 status | Notes |
|---|---|---|---|
| A1.3 | FAIL | **PASS** | Forward-compat comment added; CHECK unchanged |
| A1.5 | FAIL | **PASS** | 3 CHECKs changed to `('Yes','No','Unknown')` |
| A1.6 | FAIL | **PASS** | Schema comment added; new geocoder module with policy docstring + STATE_BOUNDS; 7/7 smoke tests pass |
| A2.3 / A2.5 | FAIL | **PASS** | `tceq_public_data_lookup` added; total 12 distinct slugs; SQL structure intact |
| A2.4 | FAIL | **PASS** | TCEQ ToS = Website Policies index (main_terms.html confirmed 404); 5 placeholder rows carry explicit "No ToS URL applicable" notes |
| A3.3 | FAIL | **PASS** | 12-line synonym-scope header comment added; synonym list unchanged |
| A3.6 | FAIL | **PASS** | 5 drinking-water exclusions added; 23/23 regex still compile |
| B2 (discovery) | n/a | **CAPTURED** | Documented in source row + audit doc; Phase-1 spike scheduled |
| Source count 11 → 12 | n/a | **PASS** | Audit doc updated with explicit clarifier |

**Items modified this pass:** 4 files

| File | Lines changed | Net delta |
|---|---|---|
| `db/migrations/0001_initial.sql` | ~25 | +12 lines (comments + casing) |
| `db/migrations/0002_source_seed.sql` | ~80 | +43 lines (new row + notes) |
| `config/facility_types.yaml` | ~22 | +18 lines (header comment + denylist) |
| `docs/source_audit_phase0.md` | ~50 | +45 lines (B2 section + source count) |
| `orchestration/geocoder.py` | **NEW** | +145 lines |

**Items NOT modified this pass:** anything that passed in v1. No regression
risk on those.

---

## Recommendation

**All seven v1 failures + the B2 discovery are now addressed.** Files are
ready for Ryan's final approval to proceed to tasks 9 and 10.

If approved, the next actions are:

1. **Task 9** — write `.github/workflows/ci.yml` (ruff + pytest + schema
   migration applied to a throwaway Postgres in CI) and
   `.github/workflows/monthly_refresh.yml` stub on a monthly cron.
2. **Task 10** — first commit + push to `main`. Per the kickoff brief, tag
   the commit `phase-1-day-1`. The commit will include:
   - the project skeleton
   - the schema and seed migrations (still not applied to Supabase)
   - the controlled-vocabulary YAML
   - the geocoder stub
   - the source audit + checkpoint-2 self-review docs
   - the GitHub Actions workflow stubs

No SQL is applied to Supabase as part of task 10. That happens only after
Ryan reviews the migrations in their final form and explicitly says
"apply." The push is a code push, not a data action.

**Holding here. No commits. No migrations applied. No push.**
