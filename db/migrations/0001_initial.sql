-- =============================================================================
-- 0001_initial.sql
-- Arch Legacy Partners — Wastewater Facility Database
-- Schema v0.1. Authored 2026-05-11 by Axiom Insights.
--
-- DO NOT APPLY until Ryan has reviewed. Run via `supabase db push` or psql
-- against the Supabase project only after sign-off.
--
-- Conventions:
--   - All timestamps are TIMESTAMPTZ. UTC ISO 8601 enforced at application
--     layer; DB stores tz-aware. No naive datetimes anywhere.
--   - `id` columns are BIGSERIAL except `canonical_facility.id` which is UUID
--     (so the canonical entity has a stable identifier independent of insert
--     order; this is the join key for CSV exports and downstream consumers).
--   - JSONB used for `raw_payload` only. Provenance is a separate relational
--     table per the locked architectural decision (no JSONB blobs on the
--     canonical row).
--   - Soft-coupling between layers: raw → link → canonical → history /
--     provenance. Raw is immutable. Canonical can be rebuilt from raw + link.
-- =============================================================================

BEGIN;

-- Enable required extensions ---------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "citext";     -- case-insensitive email / id text

-- =============================================================================
-- Reference + operational tables
-- =============================================================================

-- source -----------------------------------------------------------------------
-- Registry of every data source we pull from. Seeded by 0002_source_seed.sql.
CREATE TABLE source (
    id                    BIGSERIAL PRIMARY KEY,
    slug                  TEXT NOT NULL UNIQUE,            -- machine key e.g. "epa_echo"
    name                  TEXT NOT NULL,                   -- human-readable
    type                  TEXT NOT NULL,                   -- federal | state | county | discovery_crawl | operator_site
    base_url              TEXT,
    tos_url               TEXT,
    tos_posture           TEXT NOT NULL DEFAULT 'unknown', -- permissive | restrictive | unknown | declined
    robots_txt_status     TEXT NOT NULL DEFAULT 'unknown', -- allow | disallow | none | unknown
    notes                 TEXT,
    last_checked_at       TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT source_type_chk CHECK (type IN
        ('federal','state','county','discovery_crawl','operator_site','registry'))
);
COMMENT ON TABLE  source IS 'Registry of every data source the pipeline pulls from.';
COMMENT ON COLUMN source.tos_posture IS 'Result of ToS audit: permissive | restrictive | unknown | declined (we chose not to scrape).';

-- scraper_run ------------------------------------------------------------------
-- One row per scraper execution.
CREATE TABLE scraper_run (
    id                BIGSERIAL PRIMARY KEY,
    source_id         BIGINT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at       TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'running',     -- running | success | failed | paused_drift
    rows_in           INTEGER,
    rows_inserted     INTEGER,
    rows_updated      INTEGER,
    error_message     TEXT,
    CONSTRAINT scraper_run_status_chk CHECK (status IN
        ('running','success','failed','paused_drift'))
);
CREATE INDEX scraper_run_source_started_idx ON scraper_run (source_id, started_at DESC);
COMMENT ON TABLE scraper_run IS 'One row per scraper execution.';

-- source_signature -------------------------------------------------------------
-- Drift detection baselines. Compared against the current run to decide whether
-- to pause the scraper per the source-drift thresholds locked in the brief.
CREATE TABLE source_signature (
    id                     BIGSERIAL PRIMARY KEY,
    source_id              BIGINT NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    scraper_run_id         BIGINT NOT NULL REFERENCES scraper_run(id) ON DELETE CASCADE,
    http_status            INTEGER,
    response_byte_size     BIGINT,
    schema_hash            TEXT,
    row_count              INTEGER,
    selectors_hit_count    INTEGER,
    captured_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX source_signature_source_captured_idx ON source_signature (source_id, captured_at DESC);
COMMENT ON TABLE source_signature IS 'Per-run drift baselines: HTTP status, byte size, schema hash, row count, selector hits.';

-- =============================================================================
-- Three-tier identity model
-- =============================================================================

-- raw_facility_record ---------------------------------------------------------
-- One row per observation from a source. NEVER MODIFIED after insert.
-- Refresh is upsert on (source_id, source_record_id) — see section 8.3.
CREATE TABLE raw_facility_record (
    id                  BIGSERIAL PRIMARY KEY,
    source_id           BIGINT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
    source_record_id    TEXT NOT NULL,         -- stable per-source key (FRS, NPDES, permit#, URL hash, etc.)
    scraper_run_id      BIGINT NOT NULL REFERENCES scraper_run(id) ON DELETE RESTRICT,
    raw_payload         JSONB NOT NULL,
    payload_hash        TEXT NOT NULL,         -- sha256 of canonicalized payload — used to detect "no change" updates
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT raw_facility_record_source_recid_uk UNIQUE (source_id, source_record_id)
);
CREATE INDEX raw_facility_record_payload_hash_idx ON raw_facility_record (payload_hash);
CREATE INDEX raw_facility_record_run_idx ON raw_facility_record (scraper_run_id);
COMMENT ON TABLE raw_facility_record IS
    'Immutable observations from sources. Upserted on (source_id, source_record_id).';

-- canonical_facility ----------------------------------------------------------
-- Resolved entity. What CSV export reads. UUID-keyed.
CREATE TABLE canonical_facility (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                     TEXT,
    facility_type            TEXT,    -- canonical_type from facility_types.yaml; see facility_type_lookup
    -- Address as-given. Never stub geocoded coords.
    street                   TEXT,
    city                     TEXT,
    state                    TEXT,    -- 'TX' | 'NC' | ... (USPS code)
    zip                      TEXT,
    county                   TEXT,
    -- Coordinates stored as-given from the geocoder. NULL on geocoding failure;
    -- never stubbed. CHECK is global (-90/90, -180/180) by design — see notes
    -- on the constraints below.
    latitude                 DOUBLE PRECISION,
    longitude                DOUBLE PRECISION,
    -- Acceptance flags. Tri-state: 'Yes' | 'No' | 'Unknown' (matches the
    -- kickoff brief section 7 verbatim; CSV export reads these values directly).
    accepts_septage          TEXT,
    accepts_grease_trap      TEXT,
    accepts_portable_toilet  TEXT,
    pricing_notes            TEXT,
    phone                    TEXT,
    email                    CITEXT,
    website                  TEXT,
    -- Identifiers (useful for ID-first match overriding score-based matching).
    frs_id                   TEXT,
    npdes_id                 TEXT,
    state_permit_id          TEXT,
    first_seen_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- state: USPS 2-letter code. Regex deliberately accepts any USPS code, not
    -- only 'TX'/'NC' — the kickoff brief allows $40/state additions after v1
    -- delivery and the schema is forward-compatible without a migration per
    -- added state. (See Checkpoint-2 self-review A1.3.)
    CONSTRAINT canonical_facility_state_chk
        CHECK (state IS NULL OR state ~ '^[A-Z]{2}$'),
    -- Acceptance flag values match kickoff-brief section 7 verbatim:
    -- 'Yes' | 'No' | 'Unknown'. CSV export reads stored values directly.
    CONSTRAINT canonical_facility_accepts_septage_chk
        CHECK (accepts_septage IS NULL OR accepts_septage IN ('Yes','No','Unknown')),
    CONSTRAINT canonical_facility_accepts_grease_chk
        CHECK (accepts_grease_trap IS NULL OR accepts_grease_trap IN ('Yes','No','Unknown')),
    CONSTRAINT canonical_facility_accepts_porta_chk
        CHECK (accepts_portable_toilet IS NULL OR accepts_portable_toilet IN ('Yes','No','Unknown')),
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
);
CREATE INDEX canonical_facility_state_type_idx  ON canonical_facility (state, facility_type);
CREATE INDEX canonical_facility_county_idx      ON canonical_facility (state, county);
CREATE INDEX canonical_facility_frs_idx         ON canonical_facility (frs_id)        WHERE frs_id IS NOT NULL;
CREATE INDEX canonical_facility_npdes_idx       ON canonical_facility (npdes_id)      WHERE npdes_id IS NOT NULL;
CREATE INDEX canonical_facility_state_permit_idx ON canonical_facility (state_permit_id) WHERE state_permit_id IS NOT NULL;
COMMENT ON TABLE canonical_facility IS
    'Resolved entity. CSV export reads from here. Provenance lives separately in field_provenance.';

-- facility_record_link --------------------------------------------------------
-- Many raw → one canonical.
CREATE TABLE facility_record_link (
    id                       BIGSERIAL PRIMARY KEY,
    raw_facility_record_id   BIGINT NOT NULL REFERENCES raw_facility_record(id) ON DELETE CASCADE,
    canonical_facility_id    UUID   NOT NULL REFERENCES canonical_facility(id)  ON DELETE CASCADE,
    match_score              NUMERIC(5,2),     -- 0.00–100.00; null for ID-first overrides
    match_method             TEXT NOT NULL,    -- id_match | rapidfuzz | manual | discovery_extract
    linked_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT facility_record_link_method_chk CHECK (match_method IN
        ('id_match','rapidfuzz','manual','discovery_extract')),
    CONSTRAINT facility_record_link_raw_uk UNIQUE (raw_facility_record_id)
);
CREATE INDEX facility_record_link_canonical_idx ON facility_record_link (canonical_facility_id);
COMMENT ON TABLE facility_record_link IS
    'Many-to-one map from raw observations to the canonical entity. One canonical per raw.';

-- field_provenance ------------------------------------------------------------
-- Per-field provenance. Keyed by (canonical_facility, field, observed_at).
-- The canonical_facility row holds the *current* value; this table is the
-- audit + source-attribution history per field.
CREATE TABLE field_provenance (
    id                     BIGSERIAL PRIMARY KEY,
    canonical_facility_id  UUID NOT NULL REFERENCES canonical_facility(id) ON DELETE CASCADE,
    field_name             TEXT NOT NULL,
    value                  TEXT,            -- text representation; numeric/json fields stringify here
    source_url             TEXT,
    source_date            TIMESTAMPTZ,
    extraction_method      TEXT NOT NULL,   -- direct_scrape | llm_extracted | manual
    confidence             TEXT NOT NULL,   -- high | medium | low
    observed_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT field_provenance_method_chk CHECK (extraction_method IN
        ('direct_scrape','llm_extracted','manual')),
    CONSTRAINT field_provenance_conf_chk   CHECK (confidence IN
        ('high','medium','low'))
);
CREATE INDEX field_provenance_facility_field_idx ON field_provenance (canonical_facility_id, field_name, observed_at DESC);
CREATE INDEX field_provenance_field_idx          ON field_provenance (field_name);
COMMENT ON TABLE field_provenance IS
    'Per-field provenance: source URL, source date, extraction method, confidence. Keyed by (facility, field, observed_at).';

-- canonical_facility_history --------------------------------------------------
-- Field-level audit of changes to canonical_facility values over time.
CREATE TABLE canonical_facility_history (
    id                     BIGSERIAL PRIMARY KEY,
    canonical_facility_id  UUID NOT NULL REFERENCES canonical_facility(id) ON DELETE CASCADE,
    field_name             TEXT NOT NULL,
    old_value              TEXT,
    new_value              TEXT,
    changed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    change_source          TEXT NOT NULL    -- scraper_run:<id> | manual:<user> | resolver
);
CREATE INDEX canonical_facility_history_facility_idx ON canonical_facility_history (canonical_facility_id, changed_at DESC);
COMMENT ON TABLE canonical_facility_history IS
    'Field-level audit of changes to canonical_facility. Written by the resolver after each ingestion.';

-- =============================================================================
-- Controlled vocabulary cache, geocoding cache, LLM cache
-- =============================================================================

-- facility_type_lookup --------------------------------------------------------
-- Cached materialization of config/facility_types.yaml. Loaders use this to
-- normalize raw type strings to one of the seven canonical categories.
CREATE TABLE facility_type_lookup (
    id              BIGSERIAL PRIMARY KEY,
    canonical_type  TEXT NOT NULL,    -- one of the 7 canonical categories
    synonym         CITEXT NOT NULL,  -- case-insensitive raw string seen in a source
    source_name     TEXT,             -- optional: pin a synonym to a source if it's source-specific
    rule_kind       TEXT NOT NULL DEFAULT 'exact', -- exact | regex
    notes           TEXT,
    CONSTRAINT facility_type_lookup_uk UNIQUE (canonical_type, synonym, source_name),
    CONSTRAINT facility_type_lookup_rule_chk CHECK (rule_kind IN ('exact','regex'))
);
CREATE INDEX facility_type_lookup_synonym_idx ON facility_type_lookup (synonym);
COMMENT ON TABLE facility_type_lookup IS
    'Synonym → canonical type mapping. Mirrored from config/facility_types.yaml; loaders must use this, not inline normalization.';

-- geocoding_cache -------------------------------------------------------------
-- Address → coords cache. Hash the normalized input address.
CREATE TABLE geocoding_cache (
    address_hash      TEXT PRIMARY KEY,        -- sha256(normalized address)
    normalized_input  TEXT NOT NULL,
    lat               DOUBLE PRECISION,
    lng               DOUBLE PRECISION,
    confidence        TEXT NOT NULL,           -- high | medium | low | failed
    matched_address   TEXT,                    -- what Census echoed back
    geocoded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT geocoding_cache_conf_chk CHECK (confidence IN ('high','medium','low','failed'))
);
COMMENT ON TABLE geocoding_cache IS
    'US Census Geocoder result cache. confidence=failed means we asked and got nothing — null lat/lng. We never stub coords.';

-- llm_enrichment_cache --------------------------------------------------------
-- Anthropic Haiku response cache. Keyed by content+prompt hash so re-runs are
-- cheap and budget caps in YAML can actually be enforced.
CREATE TABLE llm_enrichment_cache (
    id              BIGSERIAL PRIMARY KEY,
    content_hash    TEXT NOT NULL,             -- sha256(search results + facility name + state)
    prompt_hash     TEXT NOT NULL,             -- sha256(prompt template + version)
    response_json   JSONB NOT NULL,
    model_id        TEXT NOT NULL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT llm_enrichment_cache_uk UNIQUE (content_hash, prompt_hash)
);
CREATE INDEX llm_enrichment_cache_created_idx ON llm_enrichment_cache (created_at DESC);
COMMENT ON TABLE llm_enrichment_cache IS
    'Anthropic Haiku enrichment cache. Hard budget caps live in YAML config; this table makes re-runs free.';

-- =============================================================================
-- Discovery crawl (Phase 4.5)
-- =============================================================================

-- discovered_url --------------------------------------------------------------
-- Queue of URLs surfaced by Brave Search for the bounded category × state crawl.
CREATE TABLE discovered_url (
    id                       BIGSERIAL PRIMARY KEY,
    source_category          TEXT NOT NULL,        -- one of the 7 canonical categories
    state                    TEXT NOT NULL,
    query                    TEXT NOT NULL,
    url                      TEXT NOT NULL,
    fetch_status             TEXT NOT NULL DEFAULT 'pending',  -- pending | fetched | failed | skipped
    content_hash             TEXT,
    classified_relevance     TEXT,                              -- relevant | unrelated | uncertain
    discovered_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fetched_at               TIMESTAMPTZ,
    CONSTRAINT discovered_url_uk UNIQUE (url, source_category, state),
    CONSTRAINT discovered_url_fetch_chk CHECK (fetch_status IN
        ('pending','fetched','failed','skipped')),
    CONSTRAINT discovered_url_rel_chk CHECK (classified_relevance IS NULL OR classified_relevance IN
        ('relevant','unrelated','uncertain'))
);
CREATE INDEX discovered_url_state_cat_idx ON discovered_url (state, source_category);
CREATE INDEX discovered_url_status_idx    ON discovered_url (fetch_status);
COMMENT ON TABLE discovered_url IS
    'Discovery-crawl queue. Bounded per category×state by YAML budget caps. URLs come from Brave Search.';

-- discovery_candidate_facility ------------------------------------------------
-- Extracted-but-unresolved candidates from discovery_url. Entity resolution
-- runs over these; high-confidence go straight to canonical, low go to review.
CREATE TABLE discovery_candidate_facility (
    id                         BIGSERIAL PRIMARY KEY,
    discovered_url_id          BIGINT NOT NULL REFERENCES discovered_url(id) ON DELETE CASCADE,
    raw_payload                JSONB NOT NULL,
    classification_confidence  TEXT NOT NULL,   -- high | medium | low
    extracted_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    review_status              TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | merged
    CONSTRAINT discovery_candidate_conf_chk   CHECK (classification_confidence IN
        ('high','medium','low')),
    CONSTRAINT discovery_candidate_review_chk CHECK (review_status IN
        ('pending','approved','rejected','merged'))
);
CREATE INDEX discovery_candidate_url_idx    ON discovery_candidate_facility (discovered_url_id);
CREATE INDEX discovery_candidate_review_idx ON discovery_candidate_facility (review_status);
COMMENT ON TABLE discovery_candidate_facility IS
    'Haiku-extracted candidate facilities from discovery URLs. Gated by review-queue before entering canonical.';

-- discovery_review_queue ------------------------------------------------------
-- Holds net-new candidates that need human review before being promoted into
-- canonical_facility. Locked decision per the brief: discovery cannot
-- auto-create net-new canonicals.
CREATE TABLE discovery_review_queue (
    id              BIGSERIAL PRIMARY KEY,
    candidate_id    BIGINT NOT NULL REFERENCES discovery_candidate_facility(id) ON DELETE CASCADE,
    hold_reason     TEXT NOT NULL,
    held_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolution      TEXT,           -- approved_new | merged_existing | rejected
    resolver        TEXT,           -- email / username of the human who resolved it
    CONSTRAINT discovery_review_resolution_chk CHECK (resolution IS NULL OR resolution IN
        ('approved_new','merged_existing','rejected'))
);
CREATE INDEX discovery_review_held_idx ON discovery_review_queue (held_at DESC) WHERE resolved_at IS NULL;
COMMENT ON TABLE discovery_review_queue IS
    'Held-for-review queue for discovery candidates that look net-new. Discovery cannot bypass this.';

COMMIT;

-- =============================================================================
-- End of 0001_initial.sql
-- =============================================================================
