# Checkpoint 2 — Self-Verification Report

**Date:** 2026-05-11T19:45:00Z (UTC)
**Auditor:** Claude Code (self)
**Working folder:** `C:\Users\getur\Axiom Insights\Arch Legacy Partners`
**Status:** Hold for Ryan's review. No fixes applied. No commits. No migrations run.

---

## Section A: File checks

### A1. db/migrations/0001_initial.sql

**A1.1 — Exactly 14 tables defined.** **PASS**

Tables found, in declaration order:

1. `source` (line 34)
2. `scraper_run` (line 54)
3. `source_signature` (line 73)
4. `raw_facility_record` (line 94)
5. `canonical_facility` (line 111)
6. `facility_record_link` (line 160)
7. `field_provenance` (line 179)
8. `canonical_facility_history` (line 201)
9. `facility_type_lookup` (line 221)
10. `geocoding_cache` (line 237)
11. `llm_enrichment_cache` (line 253)
12. `discovered_url` (line 274)
13. `discovery_candidate_facility` (line 299)
14. `discovery_review_queue` (line 320)

---

**A1.2 — `canonical_facility` UUID PK with default generator.** **PASS**

```sql
-- line 112
id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
```

`pgcrypto` extension is enabled at line 25 (`CREATE EXTENSION IF NOT EXISTS "pgcrypto"`) which supplies `gen_random_uuid()`.

---

**A1.3 — State CHECK limits to 'TX' or 'NC' (or equivalent regex).** **FAIL — deliberate divergence; awaiting Ryan's call**

Current:

```sql
-- lines 137-138
CONSTRAINT canonical_facility_state_chk
    CHECK (state IS NULL OR state ~ '^[A-Z]{2}$'),
```

The regex `^[A-Z]{2}$` accepts **any** USPS 2-letter code, not only TX/NC. This was intentional — the brief calls out $40-per-state additions after v1 delivery, and I wanted the schema to support those without a migration. But it does **not** match Ryan's check spec ("limiting values to 'TX' or 'NC' (or an equivalent regex)").

**Proposed fix (two options, Ryan to choose):**

- **Option A (recommended) — leave as is.** Document in the column comment that the broader regex is intentional and reflects the post-v1 expansion model. The brief's section 7 says state is "TX or NC" but section 5 ($40 per added state) implies the schema should be forward-compatible.
- **Option B — tighten to v1 scope.** Replace with:
  ```sql
  CHECK (state IS NULL OR state IN ('TX','NC'))
  ```
  Each new state then ships in its own micro-migration that updates this CHECK.

---

**A1.4 — `confidence` on `field_provenance` CHECK restricted to {'high','medium','low'}.** **PASS**

```sql
-- lines 191-192
CONSTRAINT field_provenance_conf_chk   CHECK (confidence IN
    ('high','medium','low'))
```

---

**A1.5 — Acceptance flag CHECKs limit to 'Yes', 'No', 'Unknown'.** **FAIL — casing mismatch**

Current schema:

```sql
-- lines 139-144
CONSTRAINT canonical_facility_accepts_septage_chk
    CHECK (accepts_septage IS NULL OR accepts_septage IN ('yes','no','unknown')),
CONSTRAINT canonical_facility_accepts_grease_chk
    CHECK (accepts_grease_trap IS NULL OR accepts_grease_trap IN ('yes','no','unknown')),
CONSTRAINT canonical_facility_accepts_porta_chk
    CHECK (accepts_portable_toilet IS NULL OR accepts_portable_toilet IN ('yes','no','unknown')),
```

The kickoff brief, section 7: "Accepts septage (**Yes / No / Unknown**)" — capitalized first letter. My schema uses lowercase. The CSV export will also need to match whatever Ryan chooses (CSV column values should be human-readable per section 8.4).

**Proposed fix:** change all three IN clauses to `('Yes','No','Unknown')` so the stored value matches the CSV export value verbatim. No reformatting at the export layer.

---

**A1.6 — Latitude/longitude CHECKs bounded to plausible TX/NC ranges.** **FAIL — global bounds, not TX/NC bounds**

Current:

```sql
-- lines 145-148
CONSTRAINT canonical_facility_lat_chk
    CHECK (latitude IS NULL OR (latitude BETWEEN -90 AND 90)),
CONSTRAINT canonical_facility_lng_chk
    CHECK (longitude IS NULL OR (longitude BETWEEN -180 AND 180))
```

Ryan asked for roughly lat 25-37, long -106 to -75. Mine allows global coordinates. Same forward-compatibility argument as A1.3 — but a tighter check catches more bad-data inserts.

**Proposed fix (two options, Ryan to choose):**

- **Option A (recommended) — keep global bounds, add an app-layer "state-consistent coords" warning** in the geocoder so a TX facility geocoded outside TX raises a `low` confidence and a review flag.
- **Option B — tighten to TX/NC envelope:**
  ```sql
  CHECK (latitude  IS NULL OR (latitude  BETWEEN 25.0 AND 37.0))
  CHECK (longitude IS NULL OR (longitude BETWEEN -106.7 AND -75.4))
  ```
  Each added state ships a migration that widens the envelope.

---

**A1.7 — All timestamp columns are `timestamptz`.** **PASS**

19 timestamp columns counted across 14 tables. Every one declared `TIMESTAMPTZ`. Sample evidence:

```sql
-- line 44       last_checked_at   TIMESTAMPTZ,
-- line 45       created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
-- line 57       started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
-- line 82       captured_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
-- line 101      ingested_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
-- line 135-136  first_seen_at / last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
-- line 185      source_date       TIMESTAMPTZ,
-- line 188      observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
-- line 207      changed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
-- line 244      geocoded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
-- line 261      created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
-- line 283-284  discovered_at / fetched_at TIMESTAMPTZ ...,
-- line 304      extracted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
-- line 324-325  held_at / resolved_at TIMESTAMPTZ ...,
```

No naive `TIMESTAMP` declarations exist anywhere in the file.

---

**A1.8 — `raw_payload` typed JSONB.** **PASS**

```sql
-- line 99
raw_payload         JSONB NOT NULL,
```

(`discovery_candidate_facility.raw_payload` line 302 and `llm_enrichment_cache.response_json` line 257 are also `JSONB`.)

---

**A1.9 — Foreign keys exist as specified.** **PASS**

```sql
-- field_provenance.canonical_facility_id → canonical_facility.id (line 181)
canonical_facility_id  UUID NOT NULL REFERENCES canonical_facility(id) ON DELETE CASCADE,

-- facility_record_link.raw_facility_record_id → raw_facility_record.id (line 162)
raw_facility_record_id   BIGINT NOT NULL REFERENCES raw_facility_record(id) ON DELETE CASCADE,

-- facility_record_link.canonical_facility_id → canonical_facility.id (line 163)
canonical_facility_id    UUID   NOT NULL REFERENCES canonical_facility(id)  ON DELETE CASCADE,
```

---

**A1.10 — Indexes on `canonical_facility(state, facility_type)` and `(state, county)`.** **PASS**

```sql
-- line 150
CREATE INDEX canonical_facility_state_type_idx  ON canonical_facility (state, facility_type);
-- line 151
CREATE INDEX canonical_facility_county_idx      ON canonical_facility (state, county);
```

---

**A1.11 — `payload_hash` on `raw_facility_record` exists, is indexed, suitable for no-change detection.** **PASS**

```sql
-- line 100
payload_hash        TEXT NOT NULL,         -- sha256 of canonicalized payload — used to detect "no change" updates
-- line 104
CREATE INDEX raw_facility_record_payload_hash_idx ON raw_facility_record (payload_hash);
```

Comment explicitly states the use case: SHA-256 of canonicalized payload for no-change detection during upserts.

---

### A2. db/migrations/0002_source_seed.sql

**A2.1 — Exactly 11 INSERT rows.** **PASS**

Tuples found in the `VALUES` block, by slug, in declaration order:

1. `epa_echo` (lines 24-37)
2. `epa_cwns_2022` (lines 38-49)
3. `state_npdes` (lines 55-67)
4. `tceq_central_registry` (lines 72-86)
5. `tceq_domestic_wastewater` (lines 87-98)
6. `nc_deq_dwr` (lines 103-114)
7. `nc_deq_dwm` (lines 115-126)
8. `county_health_placeholder` (lines 133-146)
9. `state_registries_placeholder` (lines 147-158)
10. `operator_sites_placeholder` (lines 159-171)
11. `discovery_crawl` (lines 176-189)

---

**A2.2 — `tceq_central_registry` carries `robots_txt_status = 'disallow'`.** **PASS**

```sql
-- lines 72-86 (positional fields: slug, name, type, base_url, tos_url, tos_posture, robots_txt_status, notes, last_checked_at)
'tceq_central_registry',
'TCEQ Central Registry (CRPUB)',
'state',
'https://www15.tceq.texas.gov/crpub/',
'https://www.tceq.texas.gov/agency/data/lookup-data',
'permissive',
'disallow',          -- ← robots_txt_status
'IMPORTANT: www15.tceq.texas.gov/robots.txt is "User-agent: * / Disallow: /" ...',
NOW()
```

---

**A2.3 — Separate row exists for TCEQ Public Data Lookup with its actual robots.txt posture documented.** **FAIL**

There is **no separate `tceq_public_data_lookup` source row.** Instead:

- `tceq_central_registry` row's `tos_url` column points at the Public Data Lookup landing page as a stand-in routing hint.
- `tceq_domestic_wastewater` (lines 87-98) is what occupies the "TX wastewater" data-access slot in the brief's task-7 enumeration.

Ryan's check expects a row at `tceq_public_data_lookup`. The brief (section 11.7) verbatim listed "TCEQ Central Registry, TCEQ Domestic Wastewater" as the two TCEQ rows — so my seed matches the brief — but Ryan's verification spec has shifted the second TCEQ slot to "TCEQ Public Data Lookup."

**Proposed fix (Ryan to choose):**

- **Option A (recommended) — add a 12th row** `tceq_public_data_lookup` covering `https://www.tceq.texas.gov/agency/data/lookup-data` with `robots_txt_status='allow'`. Keep `tceq_domestic_wastewater` (it's a distinct sub-program landing). This grows the seed to 12 rows.
- **Option B — rename** `tceq_domestic_wastewater` → `tceq_public_data_lookup`. Stay at 11 rows. The lookup URL becomes the `base_url`; the Domestic Wastewater landing becomes a sub-program reachable from there.

Option A is more faithful to the actual access architecture (Public Data Lookup is the umbrella catalogue; Domestic Wastewater is one of several program landings under it).

---

**A2.4 — Every row has either non-null `tos_url` or an explicit note that no ToS document exists.** **FAIL — partial**

Per-row inspection of `tos_url`:

| slug | tos_url | "no ToS exists" note? |
|---|---|---|
| `epa_echo` | `https://echo.epa.gov/resources/general-info/terms-of-service` | n/a |
| `epa_cwns_2022` | `https://www.epa.gov/web-policies-and-procedures/epa-disclaimers` | n/a |
| `state_npdes` | NULL | **no explicit note** |
| `tceq_central_registry` | `https://www.tceq.texas.gov/agency/data/lookup-data` | n/a — but this is **not a ToS URL**, it's a data-access URL |
| `tceq_domestic_wastewater` | `https://www.tceq.texas.gov/agency/data/lookup-data` | same — **not a ToS URL** |
| `nc_deq_dwr` | `https://www.deq.nc.gov/about/policies` | n/a |
| `nc_deq_dwm` | `https://www.deq.nc.gov/about/policies` | n/a |
| `county_health_placeholder` | NULL | **no explicit note** |
| `state_registries_placeholder` | NULL | **no explicit note** |
| `operator_sites_placeholder` | NULL | **no explicit note** |
| `discovery_crawl` | NULL | **no explicit note** |

Two distinct problems:

1. **Five placeholder/internal rows** have NULL `tos_url` and their notes describe placeholder status but never say verbatim "no ToS document exists for this row."
2. **The two TCEQ rows** have `tos_url` populated, but with a *data-access* URL rather than a *Terms of Use* document. I did not locate the actual TCEQ ToS document.

**Proposed fix:**

- Append a sentence to each of the five placeholder/internal `notes` fields: `"No Terms of Service URL applicable — this is a placeholder/internal row."`
- For the two TCEQ rows: replace `tos_url` with the actual TCEQ Terms of Use URL once located (a quick web check should resolve this — candidate is `https://www.tceq.texas.gov/agency/main_terms.html` or similar). If no document exists, mark NULL and add the explicit note.

---

**A2.5 — 11 slugs cover Ryan's expected list.** **FAIL — one slug mismatch**

| Expected slug | Present? |
|---|---|
| EPA ECHO | ✓ `epa_echo` |
| EPA CWNS 2022 | ✓ `epa_cwns_2022` |
| SPDES / state NPDES | ✓ `state_npdes` |
| TCEQ Central Registry | ✓ `tceq_central_registry` |
| **TCEQ Public Data Lookup** | **✗ — instead I have `tceq_domestic_wastewater`** |
| NC DEQ DWR | ✓ `nc_deq_dwr` |
| NC DEQ DWM | ✓ `nc_deq_dwm` |
| County health placeholder | ✓ `county_health_placeholder` |
| State registries placeholder | ✓ `state_registries_placeholder` |
| Operator sites placeholder | ✓ `operator_sites_placeholder` |
| `discovery_crawl` | ✓ `discovery_crawl` |

The difference: my seed has `tceq_domestic_wastewater` where Ryan's verification expects `tceq_public_data_lookup`. The brief's section 11.7 verbatim listed both TCEQ rows as I have them, so this reflects an architectural decision Ryan has now refined.

**Proposed fix:** see A2.3 — recommendation is Option A (add `tceq_public_data_lookup` as a 12th row, keep `tceq_domestic_wastewater`). If Ryan prefers exact 11-row parity with the verification spec, use Option B (rename).

---

### A3. config/facility_types.yaml

**A3.1 — Exactly seven top-level canonical categories.** **PASS**

```yaml
types:
  potw_receiving_station:           # line 40
  county_manhole_program:           # line 72
  land_application_site:            # line 98
  private_regional_septage_facility: # line 130
  composting_facility:              # line 163
  anaerobic_digester:               # line 193
  transfer_station:                 # line 222
```

---

**A3.2 — Each category has `synonyms`, `regex`, and `deny` (or equivalent) lists populated.** **PASS**

My file uses key names `synonyms`, `regex_rules`, `not_synonyms`. All 7 categories have all 3 lists populated and non-empty. Spot check (potw_receiving_station, lines 46-69):

```yaml
synonyms:        [11 entries]
regex_rules:     [4 entries]
not_synonyms:    [6 entries]
```

---

**A3.3 — POTW receiving station synonyms include "WWTP", "wastewater treatment plant", "treatment works", "POTW receiving station".** **FAIL — design tension**

All POTW-receiving-station synonyms in the file (lines 47-57):

```yaml
- "POTW receiving station"                                # ✓ matches Ryan's spec
- "POTW septage receiving station"
- "publicly owned treatment works receiving station"
- "wastewater treatment plant receiving station"          # contains the words but not standalone
- "WWTP receiving station"                                # contains "WWTP" but not standalone
- "WRP receiving station"
- "water reclamation plant receiving station"
- "municipal hauler receiving station"
- "septage receiving facility (POTW)"
- "headworks receiving station"
- "publicly owned treatment works (hauler receiving)"
```

The bare strings Ryan asked for — `"WWTP"`, `"wastewater treatment plant"`, `"treatment works"` — are **not** in the synonyms list as standalone entries. They appear only as part of receiving-station-suffixed phrases.

This was deliberate. A plain "WWTP" is not a synonym for "POTW receiving station" — it's a synonym for "POTW". Only the subset of POTWs with a manifested-load hauler receiving station belong in category 1. Adding "WWTP" as a synonym would over-match every wastewater plant.

**Proposed fix (two options, Ryan to choose):**

- **Option A (recommended) — keep my design.** Document the rationale: receiving-station synonyms are receiving-station-specific; bare facility types like "WWTP" or "POTW" are normalized to `unknown` and resolved via the acceptance-flag enrichment in Phase 4. The deny lists already exclude false positives.
- **Option B — match Ryan's spec literally.** Add the four bare strings as synonyms:
  ```yaml
  synonyms:
    - "POTW"
    - "WWTP"
    - "wastewater treatment plant"
    - "treatment works"
    - ...(plus the existing 11)
  ```
  Then add stronger deny logic to demote bare-POTW matches when the source record has no acceptance signal. Higher precision risk; more enrichment lifting.

---

**A3.4 — `land_application_site` deny list excludes terms that would falsely match composting.** **PASS**

```yaml
# lines 122-127, land_application_site
not_synonyms:
  - "transfer station"
  - "composting facility"   # ← explicit composting exclusion
  - "anaerobic digester"
  - "POTW"
  - "treatment plant"
```

---

**A3.5 — `composting_facility` deny list excludes terms that would falsely match land application sites.** **PASS**

```yaml
# lines 187-190, composting_facility
not_synonyms:
  - "anaerobic digester"
  - "transfer station"
  - "land application"      # ← explicit land-application exclusion
```

---

**A3.6 — `transfer_station` deny list excludes drinking-water-related infrastructure ("water transfer station", "drinking water", etc.).** **FAIL**

Current deny list:

```yaml
# lines 242-247, transfer_station
not_synonyms:
  - "land application"
  - "composting"
  - "anaerobic digester"
  - "POTW"
  - "manhole"
```

**No drinking-water exclusions.** And the regex `(?i)\btransfer\s*station\b` (line 239) would happily match phrases like "raw water transfer station", "drinking water transfer station", or "treated water transfer pump station." Those are drinking-water-system terms unrelated to solid-waste transfer.

**Proposed fix:** add drinking-water exclusions:

```yaml
not_synonyms:
  - "land application"
  - "composting"
  - "anaerobic digester"
  - "POTW"
  - "manhole"
  - "water transfer station"        # ← new
  - "drinking water"                # ← new
  - "raw water transfer"            # ← new
  - "treated water transfer"        # ← new
  - "potable water"                 # ← new (defensive)
```

---

**A3.7 — All regex strings compile.** **PASS**

Ran `re.compile()` over every regex string in the file:

- Total patterns: **23**
- Failures: **0**

Smoke-test code at `local/_self_review_checks.py` (gitignored); raw result at `local/_self_review_results.json`.

```text
{ "regex_smoke_test": { "total": 23, "failures": [] } }
```

---

### A4. docs/source_audit_phase0.md

**A4.1 — Section 9 source-category mapping table reproduced or referenced.** **PASS**

The doc references section 9 in its scope statement (line 5-6) and quotes section-9 ratings in a per-source "Section 9 says | This audit says" comparison column under each source. The table itself is not reproduced verbatim but is consistently referenced.

```markdown
**Scope:** Read-only reconnaissance of each candidate data source, validating
or correcting the source-category mapping in section 9 of the kickoff brief.
```

Plus per-source comparison tables (e.g., lines 109-117 for EPA ECHO).

---

**A4.2 — Two reclassifications explicitly noted.** **PASS**

```markdown
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
```

---

**A4.3 — TCEQ CRPUB robots.txt finding documented with content quoted verbatim.** **PASS (with caveat — inline form)**

```markdown
1. **TCEQ Central Registry (CRPUB) is robots-disallowed.** `www15.tceq.texas.gov/robots.txt`
   returns `User-agent: * / Disallow: /` — a full crawler ban.
```

And again under the TCEQ CRPUB section:

```markdown
| **robots.txt** | **`User-agent: * / Disallow: /`** — total crawler ban |
```

The content is quoted but compressed into an inline form (`User-agent: * / Disallow: /`). The actual file (verified live in section B1 below) is two lines:

```
User-agent: *
Disallow: /
```

The semantic content matches. The doc's inline form is a stylistic compression for readability. If Ryan wants verbatim multi-line, this is a one-line fix.

---

**A4.4 — ToS observations listed per source (one line per source minimum).** **PASS**

Every per-source section includes a "ToS posture" row in its facts table:

```markdown
| EPA ECHO                  | Permissive (US gov, public data)        |
| EPA CWNS 2022             | Permissive (US gov)                     |
| TCEQ Central Registry     | Permissive in principle (public records) |
| TCEQ Public Data Lookup   | Permissive                              |
| TCEQ Domestic Wastewater  | Permissive                              |
| NC DEQ DWR                | Permissive                              |
| NC DEQ DWM                | Permissive                              |
```

Placeholders are listed in the "Placeholders" section with their status; they have no ToS to observe yet.

---

**A4.5 — Any source flagged as no-scrape has the reason captured.** **PASS**

Only `tceq_central_registry` is flagged no-scrape. Its section header says **DO NOT SCRAPE** and the body states the reason:

```markdown
### TCEQ — Central Registry (CRPUB) — DO NOT SCRAPE

| **robots.txt** | **`User-agent: * / Disallow: /`** — total crawler ban |
...
Per locked decision 8.12, we honor the robots disallow. The same data is
public via the TCEQ Public Data Lookup downloads (see next section).
```

---

## Section B: Web checks

### B1. TCEQ robots.txt

Fetched `https://www15.tceq.texas.gov/robots.txt` live.

**Actual content (verbatim, 28 bytes, content-type `text/plain`):**

```
User-agent: *
Disallow: /
```

(CRLF line endings; trailing newline.)

- **Match with audit claim:** **PASS.** Audit doc says blanket disallow; actual file confirms.
- **Revision needed to audit doc:** No. (Optional cosmetic: replace the inline `User-agent: * / Disallow: /` with the verbatim two-line form in `docs/source_audit_phase0.md`.)

---

### B2. EPA CWNS APEX dashboard

Fetched `https://sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub` live.

- **URL reachable:** **PASS** (HTTP 200, 72,755 bytes)
- **Final URL:** `https://sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub/about`
- **Page title:** `2022 Clean Watersheds Needs Survey Dashboard`
- **APEX signature:** confirmed (`/ords/` path; APEX URL grammar)
- **Static export URL discovered:** **YES — and worth flagging to Ryan**

Static-file URLs found on the landing page (`.csv` / `.xlsx` / `.zip` / `.accdb`): **0**

However, an Oracle APEX **session-scoped download endpoint** appears in the page HTML:

```
/ords/sfdw_pub/r/sfdw/cwns_pub/download-state-zip
  ?p2_location_id=DD
  &session=16141949316918
  &cs=16CohG879kRukk8FzXPo2CpQ-Ez8rWcdORFsaZepMCPL9Rk6_loMyAvIYmSLJUHd-gEClzliHaIuYCTJmScChcA
```

**Interpretation.** The `download-state-zip` endpoint with a `p2_location_id` parameter (here `DD`, which is APEX shorthand for some default state) and a `session`+`cs` (CSRF token) pair suggests CWNS publishes per-state ZIP exports. If a fresh anonymous session can drive this endpoint with `p2_location_id` set to TX or NC, we can pull state-level CWNS data **without Playwright** — just a two-request flow:

1. `GET /ords/sfdw_pub/r/sfdw/cwns_pub/about` to obtain a session + cs token.
2. `GET /ords/.../download-state-zip?p2_location_id=<STATE>&session=<S>&cs=<CS>` to download the zip.

This is **a potential Phase-1 simplification** for the CWNS loader. I have not validated the two-request flow; that requires a test request and inspection of the returned zip's structure.

**Proposed action (Ryan to approve):**

- Add this discovery as a `notes` update on `epa_cwns_2022` and to the audit doc.
- Schedule a 30-minute investigation in Phase 1 to confirm the two-request flow before committing to Playwright. If it works, CWNS becomes the easiest source instead of the hardest.

**Phase 1 loader path if the flow doesn't work:** Playwright automation against the APEX app.

---

### B3. ECHO REST endpoint reachability

- **Entry page reachable:** **PASS.** `https://echo.epa.gov/tools/web-services` → HTTP 200, 75,652 bytes, contains REST mentions.
- **Sample query executed:** **YES.** Live `get_facilities` call against `https://echodata.epa.gov/echo/cwa_rest_services.get_facilities` with `output=JSON, p_st=TX, p_act=Y, responseset=1`:

```json
{
  "Results": {
    "Message": "Success",
    "Version": "CWA v2017-10-13 1325",
    "QueryRows": "72499",
    "QueryID": "<live integer present>"
  }
}
```

- **Response summary:** HTTP 200; QueryID present; `QueryRows = 72499` matches the Day-1 audit count exactly. The two-step `get_facilities` → `get_download` flow is still reachable and producing consistent data.

---

## Summary

| | Count |
|---|---|
| Total checks executed | 26 (A1: 11, A2: 5, A3: 7, A4: 5 — minus 2 that were merged at A2 = 28 file-level − 2 duplicated counts → 26 distinct + 3 web checks = **29 evidence points** evaluated) |
| **Passed** | **22** |
| **Failed** | **7** |

### Failures requiring Ryan's decision before approval

| ID | Item | Severity | Recommended option |
|---|---|---|---|
| A1.3 | `state` CHECK accepts any 2-letter USPS code, not just TX/NC | Low (forward-compat design) | A — leave as is, document rationale |
| A1.5 | Acceptance flag values stored lowercase, not Brief's `Yes`/`No`/`Unknown` | **Medium — visible in CSV** | Fix — change CHECK + comments to `'Yes','No','Unknown'` |
| A1.6 | Lat/long CHECKs are global bounds, not TX/NC envelope | Low (forward-compat design) | A — keep global, add app-layer warning |
| A2.3 + A2.5 | No `tceq_public_data_lookup` source row; have `tceq_domestic_wastewater` in its slot | **Medium — verification-spec mismatch** | A — add as 12th row (keep both) |
| A2.4 | Five placeholder/internal rows lack explicit "no ToS" notes; TCEQ rows have non-ToS URLs in `tos_url` | Low (compliance hygiene) | Fix — add explicit notes; replace TCEQ `tos_url` with actual TCEQ Terms-of-Use document |
| A3.3 | POTW synonyms don't include bare `"WWTP"`, `"wastewater treatment plant"`, `"treatment works"` | **Medium — design tension** | A — keep my design; document rationale (false-positive risk) |
| A3.6 | `transfer_station` deny list lacks drinking-water exclusions | Low — but easy fix | Fix — add five drinking-water exclusions |

### Additional discoveries from web checks (not failures, but worth Ryan's eyes)

- **B2 — CWNS APEX `download-state-zip` endpoint** exists. May enable per-state CWNS zip downloads via a two-request flow without Playwright. Proposed 30-minute investigation in Phase 1.

### What is NOT failing

- Schema cardinality (14 tables exact), provenance separation, three-tier identity, FK linkages, indexes, timestamptz, JSONB on raw_payload, payload_hash for idempotency, UUID PK on canonical — all PASS.
- All 23 regex patterns compile.
- TCEQ robots disallow finding is corroborated live.
- ECHO two-step REST flow is corroborated live (`QueryRows=72499`).
- Audit doc reclassifications and no-scrape reasoning are present and clear.

---

## Recommendation

**Revisions needed before approval.** Seven items above require Ryan's call. Three are documentation/casing fixes that take minutes (A1.5, A2.4, A3.6). Two are deliberate design choices that I recommend keeping but the verification spec asks otherwise (A1.3, A1.6, A3.3). One is a verification-spec mismatch where the recommended fix grows the source seed by one row (A2.3 + A2.5).

If Ryan accepts the "recommended" column above (Option A on A1.3/A1.6/A3.3, fixes on the others, add `tceq_public_data_lookup` as the 12th source row), the deltas are:

1. `0001_initial.sql` — change three CHECK clauses from `('yes','no','unknown')` to `('Yes','No','Unknown')`. Comment-only edits on lat/long and state CHECKs documenting the forward-compat intent.
2. `0002_source_seed.sql` — add one `tceq_public_data_lookup` row; append explicit "no ToS document exists" sentences to five placeholder/internal rows' notes; replace the two TCEQ rows' `tos_url` with a verified TCEQ Terms-of-Use URL (or NULL + explicit note).
3. `config/facility_types.yaml` — add five drinking-water deny-list entries to `transfer_station`; add a header comment on `potw_receiving_station` explaining the receiving-station-specific synonym scope.
4. `docs/source_audit_phase0.md` — optional cosmetic: replace inline `User-agent: * / Disallow: /` with verbatim two-line form; add a "B2 follow-up" note about the CWNS APEX `download-state-zip` endpoint.

I will not apply any of these changes until Ryan approves.

**Holding here. No commits. No migrations. No push.**
