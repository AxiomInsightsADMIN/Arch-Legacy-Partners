-- =============================================================================
-- hold_review_queue.sql
--
-- Purpose
--   Surfaces the score-band 75-91 "hold-new-canonical" bucket that the
--   Phase 3 resolver creates when a raw row scores similar-but-not-merge
--   against an existing canonical AND the 200m proximity tiebreak didn't
--   fire (either coords were missing on either side, or distance > 200m).
--   Each row in this query is a candidate duplicate the operator should
--   manually adjudicate before Phase 4 Haiku enrichment runs — otherwise
--   the same business spend gets duplicated across two canonicals.
--
--   See docs/build_log.md Phase 3 first-pass report: ~61,594 rows in
--   this band post-Phase-3 dedupe pass.
--
-- Parameters
--   None. Default is the full bucket; add LIMIT or WHERE for tractability.
--
-- Output columns
--   raw_id (raw_facility_record.id), source_slug,
--   canonical_id (the canonical the raw got linked to),
--   canonical_name, canonical_city, canonical_state,
--   match_score (75.00-91.99),
--   linked_at.
--
-- Example use case
--   Operator runs this query, exports CSV, picks the top N highest-
--   scoring rows, and either merges them manually (write the canonical_id
--   into a target column, drop one of the duplicates) or marks them as
--   confirmed distinct.
-- =============================================================================

SELECT r.id AS raw_id,
       s.slug AS source_slug,
       l.canonical_facility_id AS canonical_id,
       cf.name  AS canonical_name,
       cf.city  AS canonical_city,
       cf.state AS canonical_state,
       l.match_score,
       l.linked_at
  FROM facility_record_link l
  JOIN raw_facility_record r ON r.id = l.raw_facility_record_id
  JOIN source s ON s.id = r.source_id
  JOIN canonical_facility cf ON cf.id = l.canonical_facility_id
 WHERE l.match_method = 'rapidfuzz'
   AND l.match_score BETWEEN 75.00 AND 91.99
 ORDER BY l.match_score DESC, cf.state, cf.city, cf.name;
