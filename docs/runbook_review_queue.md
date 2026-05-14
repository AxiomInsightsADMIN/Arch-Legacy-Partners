# Runbook: Discovery Review Queue

Phase 4.5 step E workflow. Triage the discovery candidate queue and
promote / reject / merge each row at your own pace; this is an offline
process, not part of the automated monthly refresh.

**Audience:** Ryan (Axiom) and Austin's team post-handoff. Anyone with
write access to Supabase can adjudicate.

**State at handoff (2026-05-14):** 141 queue rows pending. 48 net-new
candidates + 93 borderline-match candidates. See
`docs/build_log.md` Phase 4.5 step D follow-on entry for the per-category
breakdown.

---

## 1. What the queue contains

The 188 facility candidates that Phase 4.5 discovery harvested + extracted
were routed through `resolver.entity_resolver --candidate-import` into
one of three outcomes:

| Outcome | Action taken | Queue row |
|---|---|---|
| **existing_match** (ID-first match OR RapidFuzz name score ≥ 92) | Provenance written against the existing canonical; `last_seen_at` bumped. No new canonical. | NO queue row — already merged. |
| **net_new** (no match below score 75) | New canonical inserted with `source='discovery_crawl'`. Held out of `v_all_in_scope` until approved. | YES — `hold_reason='net_new_discovery'`. |
| **hold_borderline** (RapidFuzz 75 ≤ score < 92, no proximity tiebreak) | New canonical inserted with `source='discovery_crawl'`. Held out of `v_all_in_scope`. Pointer to closest existing canonical recorded. | YES — `hold_reason='borderline_match score=... closest_canonical_id=...'`. |

The 47 existing-match candidates already enriched their target canonicals
(via `field_provenance` rows) and need no review.

---

## 2. How to read `v_discovery_review` in Supabase Table Editor

The view consolidates all the context you need to decide. Open Supabase
dashboard → Table Editor → switch to "Views" → `v_discovery_review`.

| Column | Meaning |
|---|---|
| `queue_id` | Primary key on `discovery_review_queue`. Use for SQL UPDATE. |
| `candidate_name` | Name extracted by Haiku from the source page. |
| `source_category` | Which Phase 4.5 query category surfaced this. One of `county_manhole_program`, `tx_private_regional_septage`, `tx_land_application_site`, `nc_anaerobic_digester`. |
| `classification_confidence` | Haiku's confidence: `high` / `medium` / `low`. View ordered with high first. |
| `evidence_quotation` | Literal text snippet from the source page that Haiku used to justify the extraction. |
| `source_url` | The Brave-discovered URL that produced this candidate. |
| `queue_status` | `pending` (not yet adjudicated) or `approved_new` / `merged_existing` / `rejected`. |
| `hold_reason` | `net_new_discovery` for net-new candidates; `borderline_match score=... closest_canonical_id=...` for borderlines. |
| `closest_existing_canonical_name` | For borderlines: the name of the closest existing canonical (the merge target if you resolve to `merged_existing`). NULL for net-new. |
| `closest_existing_canonical_id` | UUID of the closest existing canonical. NULL for net-new. |
| `match_score` | RapidFuzz WRatio against the closest canonical (75-91 for borderlines). NULL for net-new (no comparison ran). |
| `candidate_state` / `candidate_city` | From the candidate's Haiku-extracted payload. |
| `candidate_canonical_id` | The new canonical (under `source='discovery_crawl'`) that this candidate produced. Approving it brings this canonical into `v_all_in_scope`. |
| `held_at` / `resolved_at` / `resolver` | Audit timestamps + reviewer identity. |

**Default ordering** of the view: high confidence first, then by
`source_category` (for batching), then by `match_score DESC` (high-scoring
borderlines first within each category). Use Supabase Table Editor's
filter UI to narrow further (e.g., `queue_status = 'pending'`).

---

## 3. The three resolutions

The `resolution` column on `discovery_review_queue` accepts three
terminal values:

| Resolution | Meaning | Side effect |
|---|---|---|
| `approved_new` | The candidate represents a real new facility we want in the dataset. | `v_all_in_scope` automatically includes the canonical via its gate's IN-subquery (see [`docs/v1_scope_limitations.md`](v1_scope_limitations.md) §6). The candidate's canonical (`candidate_canonical_id`) becomes visible. |
| `merged_existing` | The candidate is the same facility as `closest_existing_canonical_id`. Don't keep the discovery-crawl canonical; absorb the candidate's evidence into the existing one. | Manual cleanup required (see SQL pattern below). The discovery canonical should be deleted and its `facility_record_link` rewired. |
| `rejected` | The candidate is not a real facility, is out of scope, or is too low-quality. | Discovery canonical stays in `canonical_facility` with `source='discovery_crawl'` but is permanently excluded from `v_all_in_scope` (the IN-subquery requires `approved_new`, not just any non-NULL resolution). Optionally delete the discovery canonical to clean up. |

---

## 4. SQL update patterns

All updates go through `discovery_review_queue`. The view automatically
reflects the new state.

### 4a. Approve a net-new candidate (the simplest path)

```sql
UPDATE discovery_review_queue
   SET resolution  = 'approved_new',
       resolved_at = NOW(),
       resolver    = 'ryan@axiominsights.example'
 WHERE id = 156;  -- queue_id from v_discovery_review
```

After this commits, run `SELECT * FROM v_all_in_scope WHERE id = <candidate_canonical_id>` —
the canonical is now visible. No further action required.

### 4b. Reject obvious noise

```sql
UPDATE discovery_review_queue
   SET resolution  = 'rejected',
       resolved_at = NOW(),
       resolver    = 'ryan@axiominsights.example'
 WHERE id = 47;
```

Optionally clean up the orphaned canonical:
```sql
DELETE FROM canonical_facility
 WHERE id = (
   SELECT candidate_canonical_id FROM discovery_review_queue
    WHERE id = 47
 );
-- Cascade: facility_record_link rows for the discovery raw also deleted.
```

### 4c. Merge a borderline candidate into the existing canonical

This is the most involved path because we need to (1) absorb the
candidate's evidence into the existing canonical's provenance, and
(2) clean up the orphaned discovery canonical.

```sql
-- 1. Mark the queue row resolved.
UPDATE discovery_review_queue
   SET resolution  = 'merged_existing',
       resolved_at = NOW(),
       resolver    = 'ryan@axiominsights.example'
 WHERE id = 151;

-- 2. Re-point the candidate's synthetic raw_facility_record at the
--    closest existing canonical. The candidate's evidence + URL is now
--    attached to the existing canonical's provenance chain.
WITH q AS (
  SELECT candidate_canonical_id, closest_existing_canonical_id
    FROM discovery_review_queue
   WHERE id = 151
)
UPDATE facility_record_link frl
   SET canonical_facility_id = q.closest_existing_canonical_id
  FROM q
 WHERE frl.canonical_facility_id = q.candidate_canonical_id;

-- 3. Migrate the field_provenance rows from the discovery canonical
--    to the existing canonical.
WITH q AS (
  SELECT candidate_canonical_id, closest_existing_canonical_id
    FROM discovery_review_queue
   WHERE id = 151
)
UPDATE field_provenance fp
   SET canonical_facility_id = q.closest_existing_canonical_id
  FROM q
 WHERE fp.canonical_facility_id = q.candidate_canonical_id;

-- 4. Drop the orphaned discovery canonical.
DELETE FROM canonical_facility
 WHERE id = (
   SELECT candidate_canonical_id FROM discovery_review_queue
    WHERE id = 151
 );

-- 5. last_seen_at bump on the merge target (cosmetic; ETL convention).
UPDATE canonical_facility
   SET last_seen_at = NOW()
 WHERE id = (
   SELECT closest_existing_canonical_id FROM discovery_review_queue
    WHERE id = 151
 );
```

For a batch merge, wrap steps 2-5 in a transaction and parameterize on
the queue_id range.

---

## 5. Recommended workflow

Read top-to-bottom in the view's default order (high confidence first,
batched by source_category, high-scoring borderlines first within each
category). Quick adjudication for the obvious cases, deep review only
where needed.

### 5a. Batch-approve high-confidence net-new

Open `v_discovery_review` filtered to `queue_status = 'pending'
AND classification_confidence = 'high' AND hold_reason = 'net_new_discovery'`.
Most of these are real facilities surfaced from authoritative public
data — examples from the 2026-05-14 step D run:

- TCEQ TLAP applications: `TWS Reeves 1`, `Reavis South Farm`,
  `El Celoso Ranch`, `Land Apply Coward Lease` — pending TCEQ permit
  applications with WQ permit numbers. High trust.
- NC DEQ press release: `Waters Farm – M&M Rivenbark Farm`,
  `Kilpatrick Farm 1, 2, 4, & 5, & Merritt Farm`, `Black Farms`,
  `Carroll's Farm` — named NC swine biogas digesters from a 2021 NC DEQ
  permit-modification press release. High trust.
- Municipal POTW pages: `Archie Elledge Wastewater Treatment Plant`
  (Winston-Salem NC), `Charlotte-Mecklenburg Utilities`,
  `Walnut Creek WWTP` (Austin TX), `Excess Flow Station` (Dallas TX) —
  authoritative city public-works pages. High trust.

For these you can SQL-batch-approve:
```sql
UPDATE discovery_review_queue
   SET resolution  = 'approved_new',
       resolved_at = NOW(),
       resolver    = 'ryan@axiominsights.example'
 WHERE classification_confidence = 'high'
   AND hold_reason = 'net_new_discovery'
   AND id IN (98, 99, 100, /* ... */);  -- explicit list after spot-check
```

### 5b. Deep-review borderlines

These are where `match_score` is 75-91 against the closest existing
canonical. The two outcomes:

- **Genuine duplicate** (e.g., `A Countywide Septic Service Inc.` at
  score 90.0 against existing `A Countywide Septic`): resolve to
  `merged_existing`.
- **Distinct business with name-fragment overlap** (e.g.,
  `All American Septic` at score 85.5 against `All Cen Tex Septic &
  Vacuum Pumping Service` — both have "Septic" but are different
  operators): resolve to `approved_new`.

Click into `evidence_quotation` and `source_url` to verify each call.
Borderlines are the bulk of the queue (93 of 141 rows) so plan ~5-10
minutes per row, batched in 30-minute sessions.

### 5c. Reject obvious noise

Some rows are extractions Haiku surfaced from low-signal pages: vague
news commentary, generic industry overviews, or operators that on closer
inspection are not in the wastewater domain. Reject these.

Examples to watch for:
- "WWTP facility in Texas" — extracted from a vendor product page with
  no actual facility name. Reject.
- Cross-state false positives where the Brave query returned a page
  about a similarly-named facility in a different state.
- Aggregator listings that name a regional company headquarters but
  the actual facility location is unclear.

```sql
UPDATE discovery_review_queue
   SET resolution  = 'rejected',
       resolved_at = NOW(),
       resolver    = 'ryan@axiominsights.example'
 WHERE id IN (/* explicit list of queue_ids */);
```

### 5d. Monitoring queue depth

Before any release, verify the pending queue is at a manageable level
or explicitly accepted:
```sql
SELECT classification_confidence, count(*)
  FROM v_discovery_review
 WHERE queue_status = 'pending'
 GROUP BY classification_confidence
 ORDER BY 1;
```

A small pending count is fine — those are the "we're not sure yet" rows
that don't appear in the customer-facing data anyway.

---

## 6. What happens automatically when you approve

The chain of effects from a single `UPDATE ... resolution='approved_new'`:

1. `v_discovery_review`'s `queue_status` flips to `approved_new`.
2. `v_all_in_scope` includes the candidate canonical via the IN-subquery
   added by migration `20260514220000_gate_views_on_discovery_review.sql`.
3. The 19 sibling views (`v_tx_in_scope`, `v_nc_in_scope`, the 14
   per-state-per-type, the 3 acceptance-flag) inherit the new row through
   their `SELECT FROM v_all_in_scope` dependency.
4. The next CSV export (manual or via monthly refresh) includes the
   approved canonical.
5. The next Phase 4 monthly Haiku acceptance-flag enrichment pass
   evaluates the approved canonical (no manual step required — it picks
   up everything in `v_all_in_scope`).

Nothing else fires — there's no email notification, no migration, no
re-run of any pipeline. The gate is just the view filter; the rest is
inheritance.

---

## 7. Cross-references

- `docs/v1_scope_limitations.md` §6 — operational cost frame (Brave
  paid tier, Anthropic budget) that bounds Phase 4.5 reruns.
- `docs/runbook_monthly_refresh.md` — what runs automatically each
  month. The discovery + review queue is **not** in the automated path
  for v1; this is a one-time backfill plus operator-discretion ongoing.
- `docs/runbook_key_rotation.md` §2 — Brave paid-tier requirement,
  necessary for any future discovery-crawl pass.
- `supabase/migrations/20260514220000_gate_views_on_discovery_review.sql`
  — the view gate.
- `supabase/migrations/20260514230000_create_discovery_review_view.sql`
  — this view.
- `resolver/entity_resolver.py` `run_candidate_import` — the function
  that produces the queue. If discovery is re-run for new candidates,
  this function deposits new pending rows.
