-- =============================================================================
-- 20260514220000_gate_views_on_discovery_review.sql
-- Arch Legacy Partners — Wastewater Facility Database
-- Phase 4.5 step D follow-on: gate access-layer views on discovery_crawl
-- review-queue approval.
--
-- Why this exists
-- ---------------
-- Phase 4.5 step D introduces a candidate-import path that inserts
-- canonical_facility rows from Brave + Haiku discovery output. Per Ryan's
-- locked rule (step E spec): those rows MUST NOT appear in the
-- customer-facing access-layer views (v_all_in_scope and its 19 siblings)
-- until a human approves them through discovery_review_queue.
--
-- Two schema changes are needed before the view gate can be expressed in
-- SQL:
--   1. canonical_facility needs a `source` column marking which load path
--      produced the row. NULL = federal/state loader. 'discovery_crawl' =
--      Phase 4.5 candidate import.
--   2. discovery_review_queue needs a canonical_facility_id column so the
--      view's IN-subquery on resolution='approved_new' can map back to
--      the canonical row.
--
-- The view-gate update affects ONLY v_all_in_scope. The 19 sibling views
-- (v_tx_in_scope, v_nc_in_scope, the 14 per-state-per-type, the 3
-- acceptance-flag) are all `SELECT * FROM v_all_in_scope WHERE ...`, so
-- they inherit the gate through their dependency chain — no per-sibling
-- view rewrites needed. This keeps the gate predicate in one place.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + CREATE OR REPLACE. Safe to
-- re-apply in CI on a fresh postgres (backfill UPDATEs are no-ops on an
-- empty canonical_facility).
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. canonical_facility.source
-- -----------------------------------------------------------------------------
ALTER TABLE canonical_facility
    ADD COLUMN IF NOT EXISTS source TEXT;

COMMENT ON COLUMN canonical_facility.source IS
    'Origin marker for the canonical row. NULL = pre-existing canonical '
    'from a federal/state loader (epa_cwns_2022, epa_echo, tceq_msw_*, '
    'nc_deq_*). ''discovery_crawl'' = inserted by Phase 4.5 candidate-import. '
    'Gated out of access-layer views until step E review-queue approval '
    '(see v_all_in_scope definition below).';

-- Backfill: set source='discovery_crawl' for any canonical currently linked
-- ONLY to discovery_crawl raw_facility_records. (If a canonical has a
-- non-discovery raw too, its identity was established by the loader; leave
-- source NULL so the view doesn't gate it.)
UPDATE canonical_facility cf
   SET source = 'discovery_crawl'
 WHERE EXISTS (
   SELECT 1 FROM facility_record_link frl
     JOIN raw_facility_record rfr ON rfr.id = frl.raw_facility_record_id
     JOIN source s ON s.id = rfr.source_id
    WHERE frl.canonical_facility_id = cf.id
      AND s.slug = 'discovery_crawl'
 )
 AND NOT EXISTS (
   SELECT 1 FROM facility_record_link frl
     JOIN raw_facility_record rfr ON rfr.id = frl.raw_facility_record_id
     JOIN source s ON s.id = rfr.source_id
    WHERE frl.canonical_facility_id = cf.id
      AND s.slug != 'discovery_crawl'
 );

-- -----------------------------------------------------------------------------
-- 2. discovery_review_queue.canonical_facility_id
-- -----------------------------------------------------------------------------
ALTER TABLE discovery_review_queue
    ADD COLUMN IF NOT EXISTS canonical_facility_id UUID
        REFERENCES canonical_facility(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS discovery_review_canonical_idx
    ON discovery_review_queue (canonical_facility_id);

COMMENT ON COLUMN discovery_review_queue.canonical_facility_id IS
    'Pointer back to the canonical_facility row this candidate produced '
    '(net-new) or was held against (borderline). Populated by the '
    'candidate-import resolver pass. Used by v_all_in_scope''s discovery '
    'gate to surface approved canonicals via IN-subquery on '
    'resolution=''approved_new''.';

-- Backfill: hydrate canonical_facility_id on existing review-queue rows
-- via the synthetic-raw chain. raw_facility_record.source_record_id is
-- 'discovery_candidate:<candidate_id>' for discovery-sourced raws (see
-- entity_resolver._insert_synthetic_raw).
UPDATE discovery_review_queue drq
   SET canonical_facility_id = (
     SELECT frl.canonical_facility_id
       FROM facility_record_link frl
       JOIN raw_facility_record rfr ON rfr.id = frl.raw_facility_record_id
      WHERE rfr.source_record_id = 'discovery_candidate:' || drq.candidate_id::text
      LIMIT 1
   )
 WHERE drq.canonical_facility_id IS NULL;

-- -----------------------------------------------------------------------------
-- 3. v_all_in_scope: gate discovery_crawl-sourced rows on review approval
-- -----------------------------------------------------------------------------
-- All 19 sibling views (v_tx_in_scope, v_nc_in_scope, 14 per-state-per-type,
-- 3 acceptance-flag) SELECT FROM v_all_in_scope, so they inherit this gate.
-- This is the single source of truth for the gate predicate.
CREATE OR REPLACE VIEW v_all_in_scope AS
  SELECT id, name, facility_type, street, city, state, zip, county,
         latitude, longitude,
         accepts_septage, accepts_grease_trap, accepts_portable_toilet,
         pricing_notes, phone, email, website,
         frs_id, npdes_id, state_permit_id,
         first_seen_at, last_seen_at
    FROM canonical_facility cf
   WHERE cf.facility_type IS NOT NULL
     AND (
       -- Federal/state loader-sourced canonicals are always in scope when typed.
       cf.source IS NULL
       OR cf.source != 'discovery_crawl'
       -- Discovery-sourced canonicals only appear when a human has approved
       -- them through the review queue. Default (resolution NULL or any
       -- non-'approved_new' value) keeps them out.
       OR cf.id IN (
         SELECT canonical_facility_id
           FROM discovery_review_queue
          WHERE resolution = 'approved_new'
            AND canonical_facility_id IS NOT NULL
       )
     );

COMMENT ON VIEW v_all_in_scope IS
  'Every canonical facility that maps to one of the seven v1 categories AND '
  'either: (a) was sourced from a federal/state loader (source IS NULL), '
  'or (b) was sourced from discovery_crawl AND has been approved through '
  'the review queue (resolution=''approved_new''). Cross-state. Excludes '
  'the ~70K NULL-type rows (mostly ECHO industrial NPDES outside v1 scope). '
  'The 19 sibling views (v_tx_in_scope, v_nc_in_scope, the 14 per-state-'
  'per-type, the 3 acceptance-flag) all SELECT FROM v_all_in_scope and '
  'inherit this gate through their dependency chain. Same row set as '
  'exports/facilities_primary.csv after step E approvals land.';

COMMIT;

-- =============================================================================
-- End of 20260514220000_gate_views_on_discovery_review.sql
-- =============================================================================
