-- =============================================================================
-- duplicate_candidates.sql
--
-- Purpose
--   Surfaces canonical_facility rows that look like duplicates by
--   case-insensitive name + state + county. Catches the cases the
--   resolver's RapidFuzz score path missed — e.g. score < 75 (rejected
--   to new canonical) but the name normalization is in fact identical
--   after lowercasing and whitespace-collapse.
--
--   The known-largest duplicate cluster surfaced by Phase 5 item 1's
--   CSV export spot-check is the NC ND single-family-residence over-
--   merge (residential FACILITY values shared with NC SF septage firm
--   addresses); the Phase 4 residential-address-pattern filter pin
--   addresses that specific case at the resolver layer. This query
--   surfaces the broader category.
--
-- Parameters
--   None. Add a HAVING n >= 3 to focus on clusters of 3+.
--
-- Output columns
--   group_size,
--   name_norm (the LOWER+TRIM-collapsed name shared across the cluster),
--   state, county,
--   canonical_id,
--   facility_type, city, street, latitude, longitude.
--
-- Example use case
--   Operator runs the query, exports CSV, sorts by group_size DESC, and
--   manually adjudicates the top clusters. Each cluster is candidates
--   for either: merging into one canonical (write into a merge-target
--   column for Phase 4 to pick up) or confirming as distinct (each row
--   is genuinely a different facility despite shared name).
-- =============================================================================

WITH norm AS (
    SELECT id,
           name,
           state,
           county,
           facility_type,
           city,
           street,
           latitude,
           longitude,
           LOWER(REGEXP_REPLACE(COALESCE(name, ''), '\s+', ' ', 'g')) AS name_norm
      FROM canonical_facility
     WHERE facility_type IS NOT NULL
       AND name IS NOT NULL
),
groups AS (
    SELECT name_norm, state, COALESCE(county, '') AS county_norm,
           COUNT(*) AS group_size
      FROM norm
     GROUP BY name_norm, state, COALESCE(county, '')
    HAVING COUNT(*) > 1
)
SELECT g.group_size,
       g.name_norm,
       n.state,
       n.county,
       n.id  AS canonical_id,
       n.facility_type,
       n.city,
       n.street,
       n.latitude,
       n.longitude
  FROM groups g
  JOIN norm n
    ON n.name_norm = g.name_norm
   AND n.state = g.state
   AND COALESCE(n.county, '') = g.county_norm
 ORDER BY g.group_size DESC, g.name_norm, n.id;
