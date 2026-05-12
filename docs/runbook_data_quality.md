# Runbook: Data Quality Investigation

This runbook covers seven issue classes the operator will hit during
normal use of the canonical facility data. Each section follows the
same shape: **Symptoms → Diagnostic → Root causes → Resolution.**

Companion docs:

- `docs/access_layer.md` — how to run the query library that drives
  most of these diagnostics
- `docs/runbook_monthly_refresh.md` — workflow-level diagnosis (drift
  pauses, scraper failures, resolver issues)
- `docs/runbook_key_rotation.md` — credential failures that manifest
  as data-quality symptoms (401 / 403 errors)
- `docs/build_log.md` — Phase 4 SFR-filter design pin (referenced
  by §5 below)

Seven issue classes:

| # | Issue | Primary diagnostic |
|---|---|---|
| 1 | Duplicate canonical facilities | `db/queries/duplicate_candidates.sql` |
| 2 | NULL or missing coordinates | `db/queries/facilities_missing_coords.sql` |
| 3 | Wrong acceptance flag value | trace `field_provenance` |
| 4 | Hold review queue management | `db/queries/hold_review_queue.sql` |
| 5 | SFR over-merge known issue | `db/queries/duplicate_candidates.sql` + `field_provenance` trace |
| 6 | Missing or sparse fields | `field_provenance` lookup |
| 7 | Source drift investigation | `exports/drift_report.json` |

---

## 1. Duplicate canonical facilities

### Symptoms

- The same facility appears twice (or more) in `v_all_in_scope`,
  `v_<state>_<facility_type>`, or `facilities_primary.csv`.
- A consumer asks "why do I see two rows for `Lloyds Portable
  Toilet Rentals` in NC?"
- The total canonical count is higher than the source-row count
  suggests it should be.

### Diagnostic

Run the duplicate-candidates query in the SQL Editor (or via psql):

```sql
-- File: db/queries/duplicate_candidates.sql
WITH norm AS (
    SELECT id, name, state, county, facility_type, city, street, latitude, longitude,
           LOWER(REGEXP_REPLACE(COALESCE(name, ''), '\s+', ' ', 'g')) AS name_norm
      FROM canonical_facility
     WHERE facility_type IS NOT NULL AND name IS NOT NULL
),
groups AS (
    SELECT name_norm, state, COALESCE(county, '') AS county_norm, COUNT(*) AS group_size
      FROM norm
     GROUP BY name_norm, state, COALESCE(county, '')
    HAVING COUNT(*) > 1
)
SELECT g.group_size, g.name_norm, n.state, n.county,
       n.id AS canonical_id, n.facility_type, n.city, n.street,
       n.latitude, n.longitude
  FROM groups g JOIN norm n ON ...
 ORDER BY g.group_size DESC, g.name_norm, n.id;
```

This returns exact-name duplicate clusters (case-insensitive,
whitespace-normalized). For each cluster, the operator decides:
merge or distinct.

Current state: returns 10 rows total (5 clusters of 2 each in the
post-Phase-3-dedupe state).

### Root causes (in observed-frequency order)

**(a) SFR over-merge — the dominant cause today.**
NC DEQ single-family-residence permits store the residential address
as the `FACILITY` name field. The resolver's score-based matcher
treats those address strings as identity and merges residential
permits with NC SF septage businesses serving the same address. The
top three clusters in the current data are all SFR over-merges. See
§5 below for the pinned Phase 4 fix.

**(b) Hold-queue (75–91 score band) entries that weren't merged.**
Two raw rows scored 75–91 (similar-but-not-merge) against an
existing canonical, and the 200m proximity tiebreak couldn't fire
(coords missing on one side, or distance > 200m). The resolver
created separate canonicals, each carrying its similar raw. These
are review candidates — see §4.

**(c) ID-first conflict** (rare; 6 observed in ECHO post-Phase-3).
One ECHO row's NPDES (`SourceID`) pointed to one canonical and the
same row's FRS (`RegistryID`) pointed to a different canonical. The
resolver kept the NPDES winner per `ID_PRECEDENCE` and logged the
conflict. The losing canonical still exists as a separate row; the
conflict means we know there's a merge target but didn't merge.

### Resolution path

Per cluster:

1. **Manually inspect the cluster** — open both canonicals via
   their `id`. Compare names, addresses, coords (if any), state
   permit IDs.

2. **Decide:**
   - **Merge** — file the cluster IDs into the Phase 4 review
     surface (the planned mechanism is the `discovery_review_queue`
     table; until Phase 4 ships, write the cluster to a working
     spreadsheet or open an issue).
   - **Distinct** — accept; the two rows are genuinely different
     facilities that happen to share a normalized name. Document
     why distinct in the issue / spreadsheet so the same cluster
     doesn't get re-reviewed next cycle.

3. **For SFR over-merges specifically (§5)**, no per-cluster action
   is needed — the Phase 4 SFR-filter pin documented in
   `docs/build_log.md` resolves them en masse on the next resolver
   rebuild.

---

## 2. NULL or missing coordinates

### Symptoms

- A facility has `latitude IS NULL OR longitude IS NULL` despite
  having a populated street address.
- Map exports show gaps where data is expected (e.g. zero markers in
  a county that has known facilities).
- The proximity-tiebreak step in the resolver never fires for a
  source.

### Diagnostic

Two queries cover the surface:

```bash
# 1) Facilities missing coords (any canonical with NULL lat/lng)
psql ... -f db/queries/facilities_missing_coords.sql

# 2) Geocoder state-mismatch attempts (Census matched but outside state)
psql ... -f db/queries/facilities_with_low_confidence_geocoding.sql
```

Current state: 1,148 facilities missing coords (mostly NC ND rows);
27 state-mismatch entries cached but not used by the resolver.

### Root causes

**(a) Census Geocoder no-match.** The address sent to Census didn't
resolve to any candidate. Per the locked policy (decision 8.5), we
do NOT stub coordinates — NULL is the correct outcome. Result is
cached in `geocoding_cache` with `confidence='failed'`.

**(b) NC ND privacy-suppressed geometry.** All 1,259 NC ND rows
arrive with NULL geom because NC DEQ deliberately stripped the
coordinates (47% are single-family residences). No amount of
geocoding recovers this — the source data has no street address
field. The `FACILITY` column sometimes contains an address-like
string (`972 New Elam Church Rd. SFR`), which the Phase 5 geocoder
backfill tried — 7% success rate. The remaining 93% are
non-recoverable from this source.

**(c) Malformed address in raw_payload.** The source published an
address that Census doesn't parse (PO boxes, intersection
descriptions like "1 mile west of Smith Rd at Hwy 47", legacy
abbreviations the geocoder doesn't recognize). The raw row landed
in `raw_facility_record` with the bad address intact; the geocoder
returned no-match.

**(d) Source row has no address at all.** Some sources publish
records with only a permit number + county. Census needs at least
a street + city to geocode.

### Resolution path

Per facility:

1. **Identify the root cause.** Open `raw_facility_record` for the
   canonical's linked raw via `facility_record_link`. Inspect the
   `raw_payload` for an address.

2. **If address is present but malformed (c):**
   - Manually correct the address string in a worksheet.
   - Re-run a single-row geocode via:
     ```python
     from orchestration.geocoder import geocode_with_state_check
     result = geocode_with_state_check(
         address='<corrected address>',
         state='<state>',
         conn=conn,  # so the result caches
     )
     print(result)
     ```
   - If the corrected address resolves with `confidence='high'`,
     update `canonical_facility.latitude` + `longitude` manually
     and add a `field_provenance` row with
     `extraction_method='manual'`, `confidence='high'`.

3. **If address is missing or NC-ND-privacy-suppressed (b, d):**
   - Accept the NULL. Per locked decision 8.5, never stub.
   - Document the gap if it affects a downstream use case (e.g. a
     county-by-county summary will show "12 facilities, 3 with
     unknown coords").

4. **If Census no-match on a complete-looking address (a):**
   - Try the geocoder once more. The Census API occasionally
     mismatches addresses; persistence helps in rare cases.
   - If still no-match, the address may not exist in the Census
     TIGER/Line dataset (recently built / private road / rural
     unaddressed). Accept NULL.

The `confidence='low'` (state-mismatch) entries are a separate
class — those addresses DID match but Census returned coords
outside the state's envelope. The resolver excludes them from
canonical enrichment. They surface via
`facilities_with_low_confidence_geocoding.sql` for operator
review; the typical resolution is **manual address inspection** to
determine if the source-provided state is correct or the
Census-returned coords are correct, then update the source row's
address or the canonical's state assignment manually.

---

## 3. Wrong acceptance flag value

### Symptoms

- A facility reports `accepts_grease_trap='Yes'` but its operator
  website explicitly says otherwise.
- A facility reports `accepts_septage='No'` but a hauler manifest
  proves they accept septage.
- A consumer reports a flagged contradiction during outreach.

This issue class is **most relevant post-Phase-4** — until Phase 4
Haiku enrichment runs, all three acceptance flags are NULL across
canonical_facility and this section's surface is empty.

### Diagnostic

Trace the value through `field_provenance` to its source:

```sql
SELECT fp.value, fp.source_url, fp.source_date,
       fp.extraction_method, fp.confidence, fp.observed_at,
       fp.canonical_facility_id
  FROM field_provenance fp
  JOIN canonical_facility cf ON cf.id = fp.canonical_facility_id
 WHERE cf.id = '<canonical_id>'
   AND fp.field_name = 'accepts_grease_trap'
 ORDER BY fp.observed_at DESC;
```

Multiple provenance rows means multiple sources attested. The
`canonical_facility.accepts_grease_trap` column carries the
"winning" value per the first-non-null-wins policy; the audit chain
shows what each source said.

For the `source_url`, click through and read the original page. For
`extraction_method='llm_extracted'`, also examine the
`llm_enrichment_cache` row that produced the value to see what
context Haiku had.

### Root causes

**(a) LLM extraction error.** Haiku read the source text and
extracted the wrong tri-state. Two sub-causes:
- Source text is ambiguous (e.g. "we may accept septage by prior
  arrangement") and Haiku rounded `Yes` from "may accept."
- Prompt template doesn't bias conservative enough — should
  default to `Unknown` when not explicit.

**(b) Source data conflict.** Two sources disagree (operator
website says yes; state permit registry says no). The first-non-null
source wins; the second's value sits in `field_provenance` as
evidence of the disagreement.

**(c) Manual override later proven wrong.** A prior operator wrote
`extraction_method='manual'` based on incomplete info.

### Resolution path

1. **Identify root cause from the trace** (a / b / c above).

2. **If LLM extraction error (a):**
   - File a tuning issue against the Phase 4 enrichment prompt.
   - **Short-term fix**: write a manual override row to
     `canonical_facility.accepts_grease_trap` (UPDATE statement) AND
     a `field_provenance` row with `extraction_method='manual'`,
     `confidence='high'`, `value='<corrected>'`. The manual
     provenance row wins on the next resolver rebuild because the
     resolver's first-non-null policy with `extraction_method`
     priority would defer to manual.
   - **Long-term fix**: re-run Phase 4 enrichment after prompt
     tuning. The `llm_enrichment_cache` will need invalidation for
     the affected canonicals (delete the cache rows for those
     `content_hash` values; next enrichment run regenerates with
     the updated prompt).

3. **If source data conflict (b):**
   - Pick the more authoritative source. Operator-website beats
     state-registry; manual hauler-manifest evidence beats both.
   - Write a manual override with `extraction_method='manual'`,
     `confidence='high'`, plus a `field_provenance` note
     documenting the conflict (e.g. value="Yes (manual; conflicts
     with TCEQ permit which says No)").
   - The conflicting source's `field_provenance` row stays — the
     audit chain shows that we knew about the conflict.

4. **If manual override later proven wrong (c):**
   - Same UPDATE pattern. Add a new manual `field_provenance` row
     with the new value; the prior manual row stays for audit.

Per locked decision 8.5, NULL is preferred over a guess. If the
truth genuinely isn't known, set the column to NULL (not `'No'`,
not `'Unknown'` — NULL).

---

## 4. Hold review queue management

### Symptoms

- The canonical count is higher than expected (more new canonicals
  than source rows added).
- A specific facility's raw row exists in `raw_facility_record` but
  its corresponding canonical has only one raw linked, when a
  manual eye-check says it should have merged with an existing
  canonical of the same name.

### Diagnostic

```bash
psql ... -f db/queries/hold_review_queue.sql
```

This returns every `facility_record_link` row in the 75–91 score
band — the "hold" candidates the resolver chose not to merge.
Current state: ~65,044 rows (the broader 75–91 band — see the
hold_review_queue.sql header for the strict-hold-only refinement
note).

### Root cause

Each row in this query is a raw that scored RapidFuzz 75–91 against
its best-match canonical. The resolver either:

- Couldn't apply the 200m proximity tiebreak (both sides had NULL
  coords, OR the distance was > 200m), so the raw became a new
  canonical, OR
- Did apply the tiebreak and merged anyway (small subset — those
  rows have `match_method='rapidfuzz'` AND linked to a canonical
  with other raws too).

The bucket includes both outcomes; the operator inspects each.

### Workflow (operator session)

A practical hold-queue review session covers ~50–200 rows. Don't
try to drain 65K in one pass.

1. **Sort by score DESC.** Highest scores (89–91) are the most
   likely false-distinct cases.

2. **For each row:**
   - Pull the canonical's full record (name, address, type, etc.).
   - Pull the raw's full record from `raw_facility_record`.
   - Decide:
     - **Confirmed merge**: the raw IS the same facility as the
       canonical. Action: update the raw's `facility_record_link`
       to point at the merge target, OR delete the singleton
       canonical and re-link.
     - **Confirmed distinct**: they're different facilities with
       similar names (e.g. two unrelated POTWs both named
       "Riverview WWTP" in different cities). Action: record
       "confirmed distinct" in your session log; no DB change.
     - **Unclear**: defer for more research; flag for re-review.

3. **Bulk operations** for confirmed merges: write a UPDATE/DELETE
   SQL script, dry-run it, then apply. Example:

   ```sql
   -- Move raw 12345 from canonical A (singleton) to canonical B
   UPDATE facility_record_link
      SET canonical_facility_id = '<canonical-B-uuid>',
          match_method = 'manual',
          match_score = NULL,
          linked_at = NOW()
    WHERE raw_facility_record_id = 12345;

   -- Delete the orphaned canonical A
   DELETE FROM canonical_facility WHERE id = '<canonical-A-uuid>';
   -- (CASCADE drops the orphan's field_provenance rows automatically.)
   ```

4. **The next resolver `--rebuild` will redo all this work.** That's
   the design: re-runs are idempotent for raw upserts but the
   resolver re-derives canonicals fresh each time. Bulk manual
   merges are most useful when the new merge target is something
   the resolver's automatic logic can't see (e.g. cross-source name
   variations that need a human pattern match). Phase 4 enrichment
   will catch many of these via Haiku's contextual judgment, so
   pre-Phase-4 hold-queue work has diminishing returns relative to
   the Phase 4 pass.

### Phase 4 dependency

Phase 4 acceptance-flag enrichment is the **bulk** resolution path
for the hold queue. Haiku reads the source pages for the candidate
canonicals and makes a contextual merge / distinct call. Operator
hold-queue sessions before Phase 4 ships are useful for the
highest-confidence cases (score 89–91, same city, similar address)
but not worth the time on the ~50K rows below score 85.

---

## 5. SFR over-merge known issue

### Symptoms

- NC DEQ single-family-residence permits appear in
  `v_nc_private_regional_septage_facility` typed as septage
  facilities.
- The top-merged-canonicals query
  (`SELECT canonical_id, COUNT(*) FROM facility_record_link
  GROUP BY 1 ORDER BY 2 DESC LIMIT 5`) returns canonicals with
  6–8 raws each, all named `<number> <street> <suffix>` patterns.
- `facilities_primary.csv` spot-checks (Phase 5 item 1 verification)
  surface canonicals like `2716 Weaver Hill Dr. SFR`,
  `1038 King Dr. SFR`, `972 New Elam Church Rd. SFR` with
  `facility_type='private_regional_septage_facility'`.

### Diagnostic

```sql
-- Top-N most-merged canonicals (likely SFR over-merges)
SELECT cf.id, cf.name, cf.state, cf.facility_type,
       COUNT(*) AS raws_linked
  FROM canonical_facility cf
  JOIN facility_record_link l ON l.canonical_facility_id = cf.id
 WHERE cf.facility_type IS NOT NULL
 GROUP BY 1, 2, 3, 4
HAVING COUNT(*) > 4
 ORDER BY raws_linked DESC;
```

Names matching `^\d+\s.+\s(SFR|SFD|RESIDENCE|HOME)` are the over-merge cluster.

### Root cause

NC DEQ's Non-Discharge Permits ArcGIS view stores residential
addresses in the `FACILITY` name field (NC DEQ removed the street
column for privacy because 47% of permits are residences). The
resolver's RapidFuzz score-based matcher treats those address
strings as identity and merges:

- The NC ND residential permit (e.g. `972 New Elam Church Rd. SFR`)
- Any NC SF septage business serving that same address
- ECHO industrial NPDES rows that happen to share the address-like
  string in `CWPName`

…all into a single `canonical_facility` typed as
`private_regional_septage_facility`. The residential permit-holder
gets conflated with the regulated hauler firm with the unrelated
NPDES discharger.

### The pinned Phase 4 fix

`docs/build_log.md` → "Phase 4 design notes (pin:
residential-address-pattern filter for resolver)" pins the fix.
Summary:

**Detection regex** (preliminary; tune against NC ND data before
Phase 4 ships):

```python
_SFR_PATTERN = re.compile(
    r"^\s*\d+\s+.+?\s+"
    r"(SFR|SFD|S\.F\.R\.|RESIDENCE|RESIDENTIAL|RES\.|SF[RW]?|HOME)"
    r"\s*$",
    re.IGNORECASE,
)
```

**Filter behavior** (target location: `resolver/_score_match.py`):

When `raw.source_slug == 'nc_deq_non_discharge_facilities'` AND the
name matches `_SFR_PATTERN`, **bypass score-based matching
entirely**. The raw still runs through `IdRegistry.lookup()` (so a
shared PERMITNUMBER would still merge), but the RapidFuzz path is
skipped. Standalone canonical creation is the correct outcome —
residential permits don't merge with business entities.

**Borderline cases** (regex matches but `PERMIT_TYPE` does NOT
carry a residential signal) write to `discovery_review_queue` with
`hold_reason='residential_filter_review'` for Phase 4 Haiku
adjudication.

**Expected drop** when the filter activates: **8–20 canonicals**
disappear (the over-merged ones split back into the residential-
permit-only canonicals plus the legitimate non-residential
canonicals they were over-merged with). The next resolver
`--rebuild` after the filter ships will show the drop.

### Resolution path

**Before Phase 4 ships:** accept the over-merges. Per the locked
"first non-null wins" policy, the residential permit name didn't
overwrite a real business name on any cross-source merge —
inspection of the SFR canonicals shows their `name` is the
SFR-pattern address. Consumers querying `v_nc_private_regional_
septage_facility` will see these rows; document them as the known
issue in any consumer-facing summary.

**After Phase 4 ships:** the filter runs automatically on every
`--rebuild`. No per-cluster operator action needed.

---

## 6. Missing or sparse fields

### Symptoms

- A `canonical_facility` column is NULL where the consumer expected
  a value (e.g. `phone` is NULL on what should be a contactable
  facility).
- `accepts_*` flags are NULL across the table (expected pre-Phase-4;
  see §3 for the post-Phase-4 wrong-value case).
- `pricing_notes` is NULL everywhere (expected pre-Phase-4).

### Diagnostic

Check `field_provenance` for the canonical and field:

```sql
SELECT fp.value, fp.source_url, fp.source_date,
       fp.extraction_method, fp.confidence
  FROM field_provenance fp
 WHERE fp.canonical_facility_id = '<canonical_id>'
   AND fp.field_name = '<column>'
 ORDER BY fp.observed_at DESC;
```

- **No rows returned** = no source ever attested to this field for
  this canonical.
- **Rows returned, all values NULL** = sources attested but with
  NULL (e.g. the column existed in the source but was blank for
  this row).
- **Rows returned with values** = the canonical has a non-NULL
  value; if you're seeing NULL on the canonical row, something
  unusual happened. Re-check the canonical row directly.

Cross-reference against `docs/sources.md` to confirm whether the
source for this canonical even publishes the field. Example: the
NC DEQ DWR Non-Discharge view has no `phone` column, so any
NC ND-only canonical will always have `phone IS NULL`.

### Root causes

**(a) Source doesn't publish the field.** Common for `pricing_notes`
(no source publishes pricing); `email` (some sources do, most
don't); `accepts_*` flags (no source publishes these tri-states
explicitly; they're Phase 4 enrichment-only).

**(b) Source row had the field blank.** Source has the column but
this particular row is empty. Audit the raw payload to confirm.

**(c) Scraper didn't capture the field.** Source has the column
populated, but the loader's normalize function in
`resolver/_normalize.py` doesn't map it to a `NormalizedRaw` field
yet. Fix: extend `_normalize_<source>()` to capture the field.

**(d) Phase 4 enrichment hasn't run for this canonical.** Common for
`accepts_*` flags and `pricing_notes` in v1.

### Resolution path

Per case:

1. **(a) Source doesn't publish:** accept the NULL. Document the
   gap if a downstream consumer asks; cross-reference to
   `docs/sources.md` for which sources cover which fields.

2. **(b) Source row blank:** accept the NULL — the source's
   authoritative answer is "unknown."

3. **(c) Scraper miss:** open `resolver/_normalize.py`, add the
   field mapping to the relevant `_normalize_<source>()` function,
   re-run the scraper (raw rows update via the
   `payload_hash <> EXCLUDED.payload_hash` upsert clause when the
   payload mutates), re-run the resolver, verify the field is now
   populated.

4. **(d) Phase 4 not run:** wait for Phase 4 enrichment (blocked on
   Anthropic API key delivery). When Phase 4 ships, the
   `accepts_*` flags and `pricing_notes` get populated as Haiku
   extracts them from operator-website content. The Phase 4 design
   pin in `docs/build_log.md` documents the LLM-enrichment-cache
   strategy.

---

## 7. Source drift investigation

### Symptoms

- `exports/drift_report.json` shows `overall_status: "pause"` on a
  monthly refresh.
- An email alert mentions a drift pause on a specific source.
- The CSV diff on the refresh PR is suspiciously large (>2,000
  lines) when no source-side announcement explains why.

### Diagnostic

Open `exports/drift_report.json` (committed to the refresh branch
or available at `exports/drift_report.json` from a local detector
run). Find the source(s) with `"status": "pause"`. Read the
`reason` and `details` blocks:

```json
"epa_echo": {
  "status": "pause",
  "reason": "row_count_drop_42_pct",
  "details": {
    "latest_signature_id": 17,
    "latest_row_count": 53000,
    "prior_signature_id": 13,
    "prior_row_count": 91500,
    "row_count_drop_fraction": 0.4208,
    "trigger": "row_count drop > 30%"
  }
}
```

Cross-check by reading the latest signatures from
`source_signature`:

```sql
SELECT ss.id, sr.status, ss.http_status, ss.response_byte_size,
       LEFT(ss.schema_hash, 12), ss.row_count, ss.captured_at
  FROM source_signature ss
  JOIN scraper_run sr ON sr.id = ss.scraper_run_id
  JOIN source s ON s.id = ss.source_id
 WHERE s.slug = '<paused-source>'
 ORDER BY ss.captured_at DESC
 LIMIT 5;
```

### The four pause triggers (locked decision 8.7)

1. **HTTP status non-200.** The source's endpoint returned a
   non-200. Check the URL by hand in a browser: 404 (source moved)
   vs 5xx (transient outage) vs 401 (credential expired).

2. **Row count drop > 30%.** The latest signature has substantially
   fewer rows than the prior one. Legitimate causes: source pruned
   closed facilities, source split into two slices, agency
   reorganization. Regression causes: scraper failed mid-load, source
   schema change caused row-skipping.

3. **Schema hash mismatch.** The source's column-set hash changed
   between signatures. **Always a real signal** — the source's data
   shape moved. Do NOT override.

4. **Byte size delta > 50%.** Often co-occurs with a row count
   change. Same diagnostic logic as #2.

### Resolution path

Per trigger:

**HTTP non-200:**

- 404 → source moved. Update the loader's URL constant; commit a
  hotfix. Re-trigger `workflow_dispatch`.
- 5xx → transient. Re-trigger once. If it still fails, the source
  has an outage; wait and re-trigger later.
- 401 → credential expired. See `docs/runbook_key_rotation.md` for
  the rotation procedure. Re-trigger after rotation.

**Row count drop > 30%:**

- **Check source announcements.** Federal sources often announce
  data-policy changes (e.g. "we pruned 30K inactive permits").
- **Read the scraper log** for the latest run. Look for skipped /
  filtered rows. The federal-loader consolidation (commit
  `9a6eb53`) fixed one false-positive pattern; verify this isn't a
  resurrection of that pattern.
- **If legitimate:** override is required because the new low-row
  signature will become the new baseline, but next refresh's
  comparison will pause again. Override procedure: see
  `docs/runbook_monthly_refresh.md` §3.2 (manually INSERT a
  synthetic "all clear" signature; escalate before doing this).
- **If regression:** do NOT override. Fix the scraper. Re-load.
  Re-trigger.

**Schema hash mismatch:**

- **Always real.** The source moved a column, renamed something,
  or added/removed a column. The normalizer expects the prior
  shape and will skip-or-misroute rows under the new shape.
- **Fix order:**
  1. Open the source URL by hand. Diff the column-set against the
     loader's expected columns.
  2. Update the loader's column mapping (and the
     `resolver/_normalize.py` extractor for that source).
  3. Re-run the loader. The new schema hash will land.
  4. Re-run the drift detector. It will pause again because the
     new schema_hash doesn't match the prior one — but THIS is
     the legitimate case where override is correct. Manually
     delete the prior signature (or accept the pause and overwrite
     the comparison baseline manually).
- **Don't try to "make the new hash match the old"** by reverting
  the normalizer; that just skips the new source rows.

**Byte size delta > 50%:**

- Same diagnostic logic as row count drop. If row count is stable
  but bytes shifted, the source changed its serialization (added a
  column, changed encoding). Read the response by hand to
  understand.

### Cross-reference

- The full diagnostic tree for drift pauses lives in
  `docs/runbook_monthly_refresh.md` §3.2.
- The detector implementation is `orchestration/drift_detector.py`.
- The locked thresholds (30% / 50%) are documented in
  `docs/build_log.md` → "Phase 5 item 3" entry.

---

## Cross-references

- **`docs/access_layer.md`** — how to run the SQL queries in the
  `db/queries/` library that drive most of these diagnostics. Use
  the Supabase SQL Editor or `psql -f <file.sql>`.
- **`docs/runbook_monthly_refresh.md`** — workflow-level diagnosis
  (drift pauses §3.2, scraper failures §3.1, resolver issues §3.3,
  CSV export issues §3.4). When a data-quality issue surfaces
  because the monthly cron didn't run cleanly, start there.
- **`docs/runbook_key_rotation.md`** — credential failures
  (401 / 403 / `psycopg2.OperationalError: password authentication
  failed`) that manifest as data-quality symptoms. Re-run the
  loaders with valid credentials, then return here for residual
  data quality issues.
- **`docs/build_log.md`** — Phase 4 SFR-filter pin (referenced by
  §5); Phase 3 prep pinned filter rules (referenced by drift
  context in §7); resolver locked decisions.

## When to escalate to Axiom Insights

Most data-quality issues are operator-resolvable per the procedures
above. Escalate when:

- A drift pause's schema_hash mismatch reveals a source-side change
  that requires more than a column-mapping update (e.g. the source
  switched from CSV to JSON, or split one source into three).
- The hold-review queue contains a cluster pattern that the SFR
  filter rule won't catch but is clearly an automatable merge (e.g.
  abbreviation drift: "Co." vs "Company" vs "Corp." across the same
  permit number).
- Phase 4 enrichment produces obviously wrong acceptance-flag
  values at scale (suggesting prompt template needs revision, not
  per-row manual override).
- A new pause condition that isn't one of the four locked
  triggers seems necessary (e.g. "the source's permit-format
  regex broke" — adding a fifth check requires architectural
  review).
