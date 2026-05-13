-- =============================================================================
-- 20260514210000_discovery_candidate_processed_state.sql
-- Arch Legacy Partners — Wastewater Facility Database
-- Phase 4.5 step D: extend discovery_candidate_facility.review_status
-- enum to add the 'processed_to_canonical' state.
--
-- Rationale: the candidate-import path in resolver/entity_resolver.py
-- needs a status to mark candidates that the resolver has already
-- processed (matched to existing canonical, inserted as net-new, or held
-- as borderline). 'pending' means "awaiting resolver processing".
-- 'approved'/'rejected'/'merged' are human-review terminal states that
-- happen LATER (Phase 4.5 step E review-queue approval). The new
-- 'processed_to_canonical' state sits between 'pending' and the human
-- terminals: resolver has done its work, the candidate is reflected on
-- canonical_facility (and possibly discovery_review_queue), but a human
-- has not yet adjudicated it.
--
-- Idempotent: DROP CONSTRAINT IF EXISTS + CREATE CONSTRAINT.
-- =============================================================================

BEGIN;

ALTER TABLE discovery_candidate_facility
    DROP CONSTRAINT IF EXISTS discovery_candidate_review_chk;

ALTER TABLE discovery_candidate_facility
    ADD CONSTRAINT discovery_candidate_review_chk
    CHECK (review_status IN ('pending','approved','rejected','merged','processed_to_canonical'));

COMMENT ON CONSTRAINT discovery_candidate_review_chk ON discovery_candidate_facility IS
    'Allowed review_status values. pending = awaiting resolver. '
    'processed_to_canonical = resolver has imported this into '
    'canonical_facility (Phase 4.5 step D). approved / rejected / merged '
    'are human-review terminal states (Phase 4.5 step E and beyond).';

COMMIT;

-- =============================================================================
-- End of 20260514210000_discovery_candidate_processed_state.sql
-- =============================================================================
