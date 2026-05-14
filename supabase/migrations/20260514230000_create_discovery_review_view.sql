-- =============================================================================
-- 20260514230000_create_discovery_review_view.sql
-- Arch Legacy Partners — Wastewater Facility Database
-- Phase 4.5 step E (deferred): consolidated review-context view.
--
-- This view is Austin / Ryan's workspace for adjudicating the discovery
-- review queue. It joins discovery_review_queue + discovery_candidate_facility
-- + discovered_url + (optional) closest existing canonical so a single SELECT
-- gives every column needed to decide approved_new / merged_existing /
-- rejected for each queue row.
--
-- See docs/runbook_review_queue.md for the workflow and SQL UPDATE patterns
-- that operate on rows surfaced by this view.
--
-- Also adds discovery_review_queue.closest_existing_canonical_id as a
-- structured pointer (previously the closest-canonical reference was
-- encoded in the hold_reason text string by entity_resolver._insert_
-- review_queue). The new column makes the join in this view clean and
-- gives the future "merged_existing" resolution path a single FK lookup
-- to attach the merged candidate's canonical to the existing one.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + CREATE OR REPLACE VIEW.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Add closest_existing_canonical_id to discovery_review_queue.
-- -----------------------------------------------------------------------------
ALTER TABLE discovery_review_queue
    ADD COLUMN IF NOT EXISTS closest_existing_canonical_id UUID
        REFERENCES canonical_facility(id) ON DELETE SET NULL;

COMMENT ON COLUMN discovery_review_queue.closest_existing_canonical_id IS
    'For borderline_match rows: pointer to the canonical_facility that '
    'scored 75-91 against the candidate name (the merge target if the '
    'reviewer resolves to ''merged_existing''). NULL for net_new_discovery '
    'rows (no existing match). Set on insert by entity_resolver.'
    '_insert_review_queue; for rows that pre-date this column, backfilled '
    'below by regex-extracting the canonical_id from the hold_reason text.';

-- -----------------------------------------------------------------------------
-- 2. Backfill from existing hold_reason strings.
--    hold_reason format for borderlines (set by entity_resolver):
--      'borderline_match score=85.5 closest_canonical_id=<uuid> closest_name=...'
-- -----------------------------------------------------------------------------
UPDATE discovery_review_queue drq
   SET closest_existing_canonical_id = (
         substring(hold_reason from 'closest_canonical_id=([0-9a-f-]{36})')
       )::UUID
 WHERE drq.closest_existing_canonical_id IS NULL
   AND drq.hold_reason LIKE 'borderline_match%'
   AND substring(hold_reason from 'closest_canonical_id=([0-9a-f-]{36})') IS NOT NULL;

CREATE INDEX IF NOT EXISTS discovery_review_closest_canonical_idx
    ON discovery_review_queue (closest_existing_canonical_id)
    WHERE closest_existing_canonical_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 3. v_discovery_review — consolidated review-context view.
--
-- One row per discovery_review_queue entry. Joins:
--   - discovery_review_queue           (queue_id, resolution, hold_reason)
--   - discovery_candidate_facility     (raw_payload with name/city/state/evidence)
--   - discovered_url                   (source_category, source URL, query)
--   - canonical_facility (twice)       (the candidate's own canonical AND the
--                                       closest_existing_canonical for borderlines)
--   - raw_facility_record + facility_record_link
--                                      (match_score from the candidate's link)
--
-- Ordering: high confidence first, then by source_category for grouping,
-- then by match_score DESC so the most-likely merges surface at the top of
-- each category.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_discovery_review AS
SELECT
    drq.id                                    AS queue_id,
    dcf.raw_payload->>'name'                  AS candidate_name,
    du.source_category                        AS source_category,
    dcf.classification_confidence             AS classification_confidence,
    dcf.raw_payload->>'evidence_quotation'    AS evidence_quotation,
    du.url                                    AS source_url,
    CASE
        WHEN drq.resolved_at IS NULL THEN 'pending'
        ELSE drq.resolution
    END                                       AS queue_status,
    drq.hold_reason                           AS hold_reason,
    closest.name                              AS closest_existing_canonical_name,
    drq.closest_existing_canonical_id         AS closest_existing_canonical_id,
    frl.match_score                           AS match_score,
    dcf.raw_payload->>'state'                 AS candidate_state,
    dcf.raw_payload->>'city'                  AS candidate_city,
    drq.canonical_facility_id                 AS candidate_canonical_id,
    drq.held_at                               AS held_at,
    drq.resolved_at                           AS resolved_at,
    drq.resolver                              AS resolver
  FROM discovery_review_queue drq
  JOIN discovery_candidate_facility dcf ON dcf.id = drq.candidate_id
  JOIN discovered_url du               ON du.id  = dcf.discovered_url_id
  -- The candidate's OWN canonical (always present after step D).
  -- Used to surface the closest-existing for borderlines via a second join.
  LEFT JOIN canonical_facility closest ON closest.id = drq.closest_existing_canonical_id
  -- match_score lives on the candidate's facility_record_link; pull it via
  -- the synthetic raw_facility_record whose source_record_id pattern is
  -- 'discovery_candidate:<id>'.
  LEFT JOIN raw_facility_record rfr
         ON rfr.source_record_id = 'discovery_candidate:' || drq.candidate_id::text
  LEFT JOIN facility_record_link frl
         ON frl.raw_facility_record_id = rfr.id
 ORDER BY
    CASE dcf.classification_confidence
        WHEN 'high'   THEN 0
        WHEN 'medium' THEN 1
        WHEN 'low'    THEN 2
        ELSE 3
    END,
    du.source_category,
    frl.match_score DESC NULLS LAST,
    drq.id;

COMMENT ON VIEW v_discovery_review IS
    'Phase 4.5 step E review workspace. One row per discovery_review_queue '
    'entry with all context needed to adjudicate approved_new / '
    'merged_existing / rejected. Ordering: classification_confidence DESC, '
    'then source_category, then match_score DESC. See '
    'docs/runbook_review_queue.md for the workflow.';

COMMIT;

-- =============================================================================
-- End of 20260514230000_create_discovery_review_view.sql
-- =============================================================================
