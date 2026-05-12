"""Phase 3 entity resolver — collapses 99,405 raw_facility_record rows into
canonical_facility entities via ID-first matching plus a RapidFuzz score
fallback with 200m proximity tiebreak.

Surface:
  - resolver.entity_resolver.run() — main entrypoint

Module layout:
  - _normalize.py    per-source raw_payload -> NormalizedRaw facade
  - _filters.py      pre-resolution row exclusion rules
  - _category_map.py raw type strings -> 7 canonical category slugs via YAML
  - _id_match.py     ID-first matching across stable identifiers
  - _score_match.py  RapidFuzz score + 200m proximity tiebreak
  - _canonicalize.py canonical_facility + facility_record_link writes
  - entity_resolver.py orchestration
"""
